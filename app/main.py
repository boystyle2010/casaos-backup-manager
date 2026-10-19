import os
import json
import time
import base64
import shutil
import hashlib
import tarfile
import threading
import subprocess
from datetime import datetime
from pathlib import Path

import docker
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

app = FastAPI(title="CasaOS Backup Manager")
templates = Jinja2Templates(directory="templates")

BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/DATA/Backups"))
APPS_BACKUP_DIR = BACKUP_ROOT / "apps"
SETTINGS_FILE = BACKUP_ROOT / "settings.json"
CLOUD_INDEX_CACHE = BACKUP_ROOT / "cloud_index_cache.json"
RCLONE_CONFIG_PATH = Path("/root/.config/rclone/rclone.conf")

APPS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
RCLONE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

IGNORED_MOUNT_SOURCES = [
    "/var/run/docker.sock",
    "/etc/localtime",
    "/etc/timezone",
    "/run/dbus",
    "/dev",
    "/sys",
    "/proc"
]

DEFAULT_SETTINGS = {
    "ha_schedule_enabled": True,
    "ha_schedule": "30 3 * * 0",
    "ha_auto_clean": True,
    "excluded_auto_apps": [],
    "backup_mode": "local_cloud",
    "schedule": "0 3 * * *",
    "schedule_enabled": False,
    "retention_count": 5,
    "encryption_enabled": False,
    "encryption_password": "",
    "rclone_remote": "gdrive:CasaOS_Backups",
    "last_cloud_sync": "Chưa đồng bộ",
    "last_cloud_error": None
}

# Quản lý tiến trình và hỗ trợ Hủy (Cancel Task)
TASK_STATE = {
    "active": False,
    "task_type": "",
    "title": "",
    "detail": "",
    "progress": 0,
    "status": "idle",
    "updated_at": 0
}
CANCEL_EVENT = threading.Event()
ACTIVE_PROC = None
PROC_LOCK = threading.Lock()

class TaskCancelledError(Exception):
    pass

def update_task(active: bool, task_type="", title="", detail="", progress=0, status="running"):
    TASK_STATE.update({
        "active": active,
        "task_type": task_type,
        "title": title,
        "detail": detail,
        "progress": progress,
        "status": status,
        "updated_at": time.time()
    })

def run_cancellable_cmd(cmd: list, timeout: int = 900):
    """Chạy lệnh subprocess cho phép bấm nút Hủy ngay lập tức từ giao diện Web"""
    global ACTIVE_PROC
    if CANCEL_EVENT.is_set():
        raise TaskCancelledError("Đã hủy tiến trình theo yêu cầu người dùng")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with PROC_LOCK:
        ACTIVE_PROC = proc

    start_t = time.time()
    try:
        while proc.poll() is None:
            if CANCEL_EVENT.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                raise TaskCancelledError("Đã hủy tiến trình theo yêu cầu người dùng")
            if time.time() - start_t > timeout:
                proc.kill()
                raise Exception("Quá thời gian thực thi (Timeout)")
            time.sleep(0.25)
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr
    finally:
        with PROC_LOCK:
            ACTIVE_PROC = None

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

def get_docker_client():
    return docker.DockerClient(base_url="unix://var/run/docker.sock", version="auto")

def sha256sum(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192 * 1024):
            if CANCEL_EVENT.is_set():
                raise TaskCancelledError("Đã hủy tiến trình")
            h.update(chunk)
    return h.hexdigest()

_DIR_SIZE_CACHE = {}

def get_dir_size_mb(path: Path, ttl: int = 45) -> float:
    now = time.time()
    key = str(path)
    if key in _DIR_SIZE_CACHE and (now - _DIR_SIZE_CACHE[key][0]) < ttl:
        return _DIR_SIZE_CACHE[key][1]
    total = 0
    if not path.exists():
        return 0.0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    val = round(total / (1024 * 1024), 2)
    _DIR_SIZE_CACHE[key] = (now, val)
    return val

def should_backup_mount(source: str) -> bool:
    for ignored in IGNORED_MOUNT_SOURCES:
        if source == ignored or source.startswith(ignored + "/"):
            return False
    return True

def get_rclone_remotes() -> list:
    if not RCLONE_CONFIG_PATH.exists():
        return []
    try:
        res = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []

def load_cloud_cache() -> dict:
    if CLOUD_INDEX_CACHE.exists():
        try:
            with open(CLOUD_INDEX_CACHE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cloud_cache(cache: dict):
    with open(CLOUD_INDEX_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

def refresh_cloud_cache():
    settings = load_settings()
    remote = settings.get("rclone_remote", "").strip().rstrip("/")
    if not remote or not get_rclone_remotes():
        return load_cloud_cache()
    try:
        cmd = ["rclone", "lsf", f"{remote}/apps", "-R", "--files-only", "--include", "manifest.json"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return load_cloud_cache()

        cache = load_cloud_cache()
        found_keys = set()
        for line in res.stdout.splitlines():
            parts = line.strip().split("/")
            if len(parts) >= 3 and parts[-1] == "manifest.json":
                app_name, ver_id = parts[0], parts[1]
                key = f"{app_name}/{ver_id}"
                found_keys.add(key)
                if key not in cache:
                    local_mf = APPS_BACKUP_DIR / app_name / ver_id / "manifest.json"
                    if local_mf.exists():
                        try:
                            cache[key] = json.loads(local_mf.read_text())
                        except Exception:
                            pass
                    else:
                        cat_res = subprocess.run(
                            ["rclone", "cat", f"{remote}/apps/{app_name}/{ver_id}/manifest.json"],
                            capture_output=True, text=True, timeout=15
                        )
                        if cat_res.returncode == 0:
                            try:
                                cache[key] = json.loads(cat_res.stdout)
                            except Exception:
                                pass

        clean_cache = {k: v for k, v in cache.items() if k in found_keys}
        save_cloud_cache(clean_cache)
        return clean_cache
    except Exception:
        return load_cloud_cache()

def get_merged_backups():
    cloud_cache = load_cloud_cache()
    merged = {}

    if APPS_BACKUP_DIR.exists():
        for app_dir in sorted(APPS_BACKUP_DIR.iterdir()):
            if app_dir.is_dir():
                for ver_dir in sorted(app_dir.iterdir(), reverse=True):
                    mf = ver_dir / "manifest.json"
                    if mf.exists():
                        try:
                            data = json.loads(mf.read_text())
                            key = f"{data['app_name']}/{data['version_id']}"
                            data["on_ssd"] = True
                            data["on_cloud"] = key in cloud_cache
                            merged[key] = data
                        except Exception:
                            pass

    for key, c_data in cloud_cache.items():
        if key not in merged and isinstance(c_data, dict) and "app_name" in c_data:
            item = c_data.copy()
            item["on_ssd"] = False
            item["on_cloud"] = True
            merged[key] = item

    return sorted(merged.values(), key=lambda x: x.get("version_id", ""), reverse=True)

def discover_containers(merged_backups=None):
    if merged_backups is None:
        merged_backups = get_merged_backups()
    client = get_docker_client()
    containers = client.containers.list(all=True)
    result = []
    for c in containers:
        name = c.name.lstrip("/")
        if name == "casaos-backup-manager":
            continue
        attrs = c.attrs
        mounts = attrs.get("Mounts", [])
        data_mounts = []
        for m in mounts:
            src = m.get("Source", "")
            dst = m.get("Destination", "")
            mtype = m.get("Type", "bind")
            if should_backup_mount(src):
                data_mounts.append({"type": mtype, "source": src, "destination": dst})

        app_snaps = [b for b in merged_backups if b.get("app_name") == name]
        ssd_count = sum(1 for b in app_snaps if b.get("on_ssd"))
        cloud_count = sum(1 for b in app_snaps if b.get("on_cloud"))

        result.append({
            "id": c.short_id,
            "name": name,
            "image": attrs.get("Config", {}).get("Image", "unknown"),
            "status": c.status,
            "network_mode": attrs.get("HostConfig", {}).get("NetworkMode", "default"),
            "data_mounts": data_mounts,
            "backup_count": len(app_snaps),
            "ssd_count": ssd_count,
            "cloud_count": cloud_count,
            "has_ssd": ssd_count > 0,
            "has_cloud": cloud_count > 0,
            "latest_backup": app_snaps[0]["timestamp_str"] if app_snaps else "Chưa có"
        })
    return result

def apply_retention(app_name: str, keep_count: int):
    if keep_count <= 0:
        return
    settings = load_settings()
    remote = settings.get("rclone_remote", "").strip().rstrip("/")

    app_dir = APPS_BACKUP_DIR / app_name
    if app_dir.exists():
        versions = sorted([d for d in app_dir.iterdir() if d.is_dir()], reverse=True)
        for old_dir in versions[keep_count:]:
            shutil.rmtree(old_dir, ignore_errors=True)

    if remote and get_rclone_remotes():
        try:
            res = subprocess.run(
                ["rclone", "lsf", f"{remote}/apps/{app_name}", "--dirs-only"],
                capture_output=True, text=True, timeout=20
            )
            if res.returncode == 0:
                cloud_vers = sorted([v.strip().rstrip("/") for v in res.stdout.splitlines() if v.strip()], reverse=True)
                cache = load_cloud_cache()
                for old_ver in cloud_vers[keep_count:]:
                    subprocess.run(
                        ["rclone", "purge", f"{remote}/apps/{app_name}/{old_ver}"],
                        capture_output=True, text=True, timeout=60
                    )
                    cache.pop(f"{app_name}/{old_ver}", None)
                save_cloud_cache(cache)
        except Exception:
            pass

# ================= PUSH & PULL CLOUD (CÓ HỖ TRỢ HỦY LỆNH) =================
def sync_to_cloud(app_name: str = "ALL", update_progress: bool = True):
    settings = load_settings()
    remote = settings.get("rclone_remote", "").strip().rstrip("/")
    if not remote:
        return False, "Chưa nhập đường dẫn Rclone Remote"
    if not RCLONE_CONFIG_PATH.exists() or RCLONE_CONFIG_PATH.stat().st_size == 0:
        msg = "Chưa có cấu hình rclone.conf"
        settings["last_cloud_error"] = msg
        save_settings(settings)
        return False, msg

    try:
        target_label = "Tất cả ứng dụng" if app_name == "ALL" else app_name
        if update_progress:
            update_task(True, "cloud_sync", f"Đang đẩy lên Cloud ({target_label})...", f"Đồng bộ tới {remote}", 35, "running")

        if app_name == "ALL":
            src_path = str(BACKUP_ROOT)
            dst_path = remote
        else:
            local_app_dir = APPS_BACKUP_DIR / app_name
            if not local_app_dir.exists():
                msg = f"Ứng dụng [{app_name}] chưa có bản sao lưu nào trên SSD!"
                if update_progress:
                    update_task(False, "cloud_sync", "Không có dữ liệu", msg, 100, "error")
                return False, msg
            src_path = str(local_app_dir)
            dst_path = f"{remote}/apps/{app_name}"

        cmd = ["rclone", "copy", src_path, dst_path, "--transfers", "4", "--exclude", "cloud_index_cache.json"]
        rc, out, err = run_cancellable_cmd(cmd, timeout=900)
        if rc == 0:
            if update_progress:
                update_task(True, "cloud_sync", "Đang cập nhật danh mục Cloud...", "Lưu trạng thái đồng bộ...", 85, "running")
            cache = load_cloud_cache()
            scan_dirs = [APPS_BACKUP_DIR / app_name] if app_name != "ALL" else (list(APPS_BACKUP_DIR.iterdir()) if APPS_BACKUP_DIR.exists() else [])
            for a_dir in scan_dirs:
                if a_dir.exists() and a_dir.is_dir():
                    for ver_dir in a_dir.iterdir():
                        mf = ver_dir / "manifest.json"
                        if mf.exists():
                            try:
                                cache[f"{a_dir.name}/{ver_dir.name}"] = json.loads(mf.read_text())
                            except Exception:
                                pass
            save_cloud_cache(cache)

            settings["last_cloud_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            settings["last_cloud_error"] = None
            save_settings(settings)
            if update_progress:
                update_task(False, "cloud_sync", "Đẩy lên Cloud hoàn tất!", f"Đã đồng bộ [{target_label}] lên {remote}", 100, "completed")
            return True, f"Đã đồng bộ [{target_label}] lên {remote} thành công!"
        else:
            err_msg = err.strip() or "Lỗi rclone"
            settings["last_cloud_error"] = err_msg
            save_settings(settings)
            if update_progress:
                update_task(False, "cloud_sync", "Lỗi đồng bộ Cloud", err_msg, 100, "error")
            return False, err_msg
    except TaskCancelledError as ce:
        if update_progress:
            update_task(False, "cloud_sync", "Đã hủy đồng bộ Cloud", str(ce), 100, "cancelled")
        return False, str(ce)
    except Exception as e:
        settings["last_cloud_error"] = str(e)
        save_settings(settings)
        if update_progress:
            update_task(False, "cloud_sync", "Lỗi đồng bộ Cloud", str(e), 100, "error")
        return False, str(e)

def pull_from_cloud_worker(app_name: str = "ALL"):
    settings = load_settings()
    remote = settings.get("rclone_remote", "").strip().rstrip("/")
    target_label = "Tất cả ứng dụng" if app_name == "ALL" else app_name
    try:
        update_task(True, "cloud_pull", f"Đang tải từ Cloud ({target_label})...", f"Kéo dữ liệu từ {remote}", 30, "running")
        if app_name == "ALL":
            src_path = f"{remote}/apps"
            dst_path = str(APPS_BACKUP_DIR)
        else:
            src_path = f"{remote}/apps/{app_name}"
            dst_dir = APPS_BACKUP_DIR / app_name
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_path = str(dst_dir)

        cmd = ["rclone", "copy", src_path, dst_path, "--transfers", "4"]
        rc, out, err = run_cancellable_cmd(cmd, timeout=900)
        if rc == 0:
            update_task(True, "cloud_pull", "Đang cập nhật danh mục...", "Quét các gói vừa tải về...", 85, "running")
            refresh_cloud_cache()
            update_task(False, "cloud_pull", "Tải từ Cloud hoàn tất!", f"Đã tải [{target_label}] về SSD thành công", 100, "completed")
        else:
            update_task(False, "cloud_pull", "Lỗi tải từ Cloud", err.strip() or "Không tìm thấy dữ liệu trên Cloud", 100, "error")
    except TaskCancelledError as ce:
        update_task(False, "cloud_pull", "Đã hủy tải từ Cloud", str(ce), 100, "cancelled")
    except Exception as e:
        update_task(False, "cloud_pull", "Lỗi tải từ Cloud", str(e), 100, "error")

# ================= BACKUP CORE =================
def backup_single_app(app_name: str, manage_task_state: bool = True) -> dict:
    if CANCEL_EVENT.is_set():
        raise TaskCancelledError("Đã hủy tiến trình sao lưu")

    client = get_docker_client()
    try:
        container = client.containers.get(app_name)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy container {app_name}")

    settings = load_settings()
    now = datetime.now()
    ver_id = now.strftime("%Y%m%d_%H%M%S")
    target_dir = APPS_BACKUP_DIR / app_name / ver_id
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        if manage_task_state:
            update_task(True, "backup", f"Đang sao lưu {app_name}...", "Trích xuất cấu hình container_spec.json", 15, "running")

        attrs = container.attrs
        spec_file = target_dir / "container_spec.json"
        with open(spec_file, "w") as f:
            json.dump(attrs, f, indent=2)

        mounts = attrs.get("Mounts", [])
        archive_path = target_dir / "appdata.tar.gz"
        backed_up_mounts = []

        if manage_task_state:
            update_task(True, "backup", f"Đang nén dữ liệu {app_name}...", "Đang đóng gói các thư mục AppData", 40, "running")

        if app_name == "homeassistant" and settings.get("ha_auto_clean", True):
            ha_cfg = Path("/DATA/AppData/homeassistant/config")
            if ha_cfg.exists():
                for pat in ["*sync-conflict*", "*.log.1", "*.log.fault", "OZW_Log.txt"]:
                    for junk in ha_cfg.rglob(pat):
                        try:
                            if junk.is_file(): junk.unlink()
                        except Exception: pass
                for pyc in ha_cfg.rglob("__pycache__"):
                    try:
                        if pyc.is_dir(): shutil.rmtree(pyc, ignore_errors=True)
                    except Exception: pass

        with tarfile.open(archive_path, "w:gz") as tar:
            for idx, m in enumerate(mounts):
                if CANCEL_EVENT.is_set():
                    raise TaskCancelledError("Đã hủy tiến trình sao lưu")
                src = m.get("Source", "")
                dst = m.get("Destination", "")
                if should_backup_mount(src) and os.path.exists(src):
                    arcname = f"mount_{idx}"
                    tar.add(src, arcname=arcname)
                    backed_up_mounts.append({
                        "arcname": arcname,
                        "source": src,
                        "destination": dst,
                        "type": m.get("Type", "bind")
                    })

        final_file = archive_path
        encrypted = False

        if settings.get("encryption_enabled") and settings.get("encryption_password"):
            if manage_task_state:
                update_task(True, "backup", f"Đang mã hóa {app_name}...", "Mã hóa chuẩn AES-256-CBC", 65, "running")
            enc_path = target_dir / "appdata.tar.gz.enc"
            pwd = settings["encryption_password"]
            cmd = [
                "openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2",
                "-in", str(archive_path), "-out", str(enc_path),
                "-pass", f"pass:{pwd}"
            ]
            rc, _, _ = run_cancellable_cmd(cmd, timeout=300)
            if rc == 0 and enc_path.exists():
                archive_path.unlink()
                final_file = enc_path
                encrypted = True

        checksum = sha256sum(final_file)
        size_mb = round(final_file.stat().st_size / (1024 * 1024), 2)

        manifest = {
            "app_name": app_name,
            "version_id": ver_id,
            "timestamp_str": now.strftime("%Y-%m-%d %H:%M:%S"),
            "image": attrs.get("Config", {}).get("Image", ""),
            "archive_file": final_file.name,
            "encrypted": encrypted,
            "sha256": checksum,
            "size_mb": size_mb,
            "mounts_map": backed_up_mounts
        }
        with open(target_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)

        if settings.get("backup_mode") in ["cloud", "local_cloud"]:
            if manage_task_state:
                update_task(True, "backup", f"Đang tải {app_name} lên Cloud...", f"Đồng bộ gói {size_mb} MB lên Cloud", 80, "running")
            ok, msg = sync_to_cloud(app_name=app_name, update_progress=False)
            if CANCEL_EVENT.is_set():
                raise TaskCancelledError("Đã hủy khi đang tải lên Cloud")

            if ok and settings.get("backup_mode") == "cloud":
                shutil.rmtree(target_dir, ignore_errors=True)

        if manage_task_state:
            update_task(True, "backup", f"Đang dọn bản cũ ({app_name})...", "Áp dụng chính sách Retention trên SSD & Cloud", 92, "running")
        apply_retention(app_name, int(settings.get("retention_count", 5)))

        if manage_task_state:
            update_task(False, "backup", f"Hoàn tất sao lưu {app_name}!", f"Phiên bản {ver_id} ({size_mb} MB)", 100, "completed")

        return manifest
    except TaskCancelledError:
        shutil.rmtree(target_dir, ignore_errors=True)
        if manage_task_state:
            update_task(False, "backup", "Đã hủy sao lưu", f"Đã dọn dẹp file tạm của {app_name}", 100, "cancelled")
        raise

def run_backup_task(app_name):
    try:
        if isinstance(app_name, list):
            target_names = [a for a in app_name if a]
            total = len(target_names)
            for idx, name in enumerate(target_names, start=1):
                if CANCEL_EVENT.is_set():
                    raise TaskCancelledError("Đã hủy sao lưu hàng loạt")
                pct = int(((idx - 1) / max(total, 1)) * 85) + 5
                update_task(True, "backup", f"Đang sao lưu ({idx}/{total}): {name}", "Đóng gói cấu hình & dữ liệu...", pct, "running")
                backup_single_app(name, manage_task_state=False)
            update_task(False, "backup", "Đã sao lưu các ứng dụng đã chọn!", f"Hoàn tất {total}/{total} ứng dụng", 100, "completed")
        elif app_name in ("ALL", "AUTO"):
            containers = discover_containers()
            if app_name == "AUTO":
                st = load_settings()
                excluded = set(st.get("excluded_auto_apps", []))
                if st.get("ha_schedule_enabled"):
                    excluded.add("homeassistant")
                containers = [c for c in containers if c["name"] not in excluded]
            total = len(containers)
            for idx, c in enumerate(containers, start=1):
                if CANCEL_EVENT.is_set():
                    raise TaskCancelledError("Đã hủy sao lưu hàng loạt")
                pct = int(((idx - 1) / max(total, 1)) * 85) + 5
                update_task(True, "backup", f"Đang sao lưu ({idx}/{total}): {c['name']}", "Đóng gói cấu hình & dữ liệu...", pct, "running")
                backup_single_app(c["name"], manage_task_state=False)
            update_task(False, "backup", "Đã sao lưu toàn bộ ứng dụng!", f"Hoàn tất {total}/{total} ứng dụng", 100, "completed")
        else:
            backup_single_app(app_name, manage_task_state=True)
    except TaskCancelledError as ce:
        update_task(False, "backup", "Đã hủy sao lưu", str(ce), 100, "cancelled")
    except Exception as e:
        update_task(False, "backup", "Lỗi trong quá trình sao lưu", str(e), 100, "error")

# ================= RESTORE CORE =================
def recreate_container_from_spec(client, app_name: str, spec: dict):
    cfg = spec.get("Config", {})
    host_cfg = spec.get("HostConfig", {})
    image_name = cfg.get("Image")

    net_mode = host_cfg.get("NetworkMode", "bridge")
    if net_mode not in ["bridge", "host", "none", "default"] and not net_mode.startswith("container:"):
        existing_nets = [n.name for n in client.networks.list()]
        if net_mode not in existing_nets:
            client.networks.create(net_mode, driver="bridge")

    try:
        client.images.get(image_name)
    except docker.errors.ImageNotFound:
        client.images.pull(image_name)

    clean_host_cfg = {
        "Binds": host_cfg.get("Binds"),
        "PortBindings": host_cfg.get("PortBindings"),
        "RestartPolicy": host_cfg.get("RestartPolicy"),
        "NetworkMode": net_mode,
        "Devices": host_cfg.get("Devices"),
        "Privileged": host_cfg.get("Privileged", False),
        "CapAdd": host_cfg.get("CapAdd"),
        "CapDrop": host_cfg.get("CapDrop"),
        "ExtraHosts": host_cfg.get("ExtraHosts"),
    }
    clean_host_cfg = {k: v for k, v in clean_host_cfg.items() if v is not None}
    exposed_ports = list(cfg.get("ExposedPorts", {}).keys()) if cfg.get("ExposedPorts") else None

    res = client.api.create_container(
        image=image_name,
        name=app_name,
        hostname=cfg.get("Hostname"),
        user=cfg.get("User"),
        command=cfg.get("Cmd"),
        entrypoint=cfg.get("Entrypoint"),
        environment=cfg.get("Env"),
        labels=cfg.get("Labels"),
        ports=exposed_ports,
        working_dir=cfg.get("WorkingDir"),
        host_config=clean_host_cfg,
        detach=True
    )
    client.api.start(res["Id"])

def restore_single_app(app_name: str, version_id: str, source: str = "ssd", recreate_container: bool = False) -> dict:
    CANCEL_EVENT.clear()
    try:
        ver_dir = APPS_BACKUP_DIR / app_name / version_id
        settings = load_settings()
        remote = settings.get("rclone_remote", "").strip().rstrip("/")

        if source == "cloud" or not (ver_dir / "manifest.json").exists():
            if not remote:
                raise HTTPException(status_code=400, detail="Chưa cấu hình Rclone Remote để tải từ Cloud")
            update_task(True, "restore", f"Đang tải [{app_name}] từ Cloud...", f"Tải phiên bản {version_id} về SSD", 25, "running")
            ver_dir.mkdir(parents=True, exist_ok=True)
            cmd = ["rclone", "copy", f"{remote}/apps/{app_name}/{version_id}", str(ver_dir), "--transfers", "4"]
            rc, _, err = run_cancellable_cmd(cmd, timeout=600)
            if rc != 0:
                raise HTTPException(status_code=400, detail=f"Không thể tải từ Cloud: {err.strip()}")

        manifest_file = ver_dir / "manifest.json"
        spec_file = ver_dir / "container_spec.json"

        if not manifest_file.exists() or not spec_file.exists():
            raise HTTPException(status_code=404, detail="Không tìm thấy gói backup trên cả SSD và Cloud")

        manifest = json.loads(manifest_file.read_text())
        spec = json.loads(spec_file.read_text())

        archive_file = ver_dir / manifest["archive_file"]
        if not archive_file.exists():
            raise HTTPException(status_code=404, detail="File dữ liệu nén không tồn tại")

        update_task(True, "restore", f"Kiểm tra toàn vẹn [{app_name}]...", "Xác thực mã băm SHA-256", 50, "running")
        current_hash = sha256sum(archive_file)
        if current_hash != manifest.get("sha256"):
            raise HTTPException(status_code=400, detail="Lỗi toàn vẹn dữ liệu: Mã SHA-256 không khớp!")

        tar_to_extract = archive_file

        if manifest.get("encrypted"):
            pwd = settings.get("encryption_password", "")
            if not pwd:
                raise HTTPException(status_code=400, detail="Bản sao lưu này được mã hóa. Hãy nhập mật khẩu ở tab Cài đặt!")
            update_task(True, "restore", f"Đang giải mã [{app_name}]...", "Giải mã AES-256-CBC", 65, "running")
            dec_path = ver_dir / "decrypted_temp.tar.gz"
            cmd = [
                "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
                "-in", str(archive_file), "-out", str(dec_path),
                "-pass", f"pass:{pwd}"
            ]
            rc, _, _ = run_cancellable_cmd(cmd, timeout=300)
            if rc != 0:
                raise HTTPException(status_code=400, detail="Giải mã thất bại: Sai mật khẩu!")
            tar_to_extract = dec_path

        update_task(True, "restore", f"Đang bung dữ liệu [{app_name}]...", "Tạm dừng container và khôi phục AppData", 80, "running")
        client = get_docker_client()
        existing_container = None
        try:
            existing_container = client.containers.get(app_name)
            if existing_container.status == "running":
                existing_container.stop(timeout=15)
        except docker.errors.NotFound:
            pass

        temp_extract = ver_dir / "extract_tmp"
        shutil.rmtree(temp_extract, ignore_errors=True)
        temp_extract.mkdir(parents=True, exist_ok=True)

        try:
            with tarfile.open(tar_to_extract, "r:gz") as tar:
                tar.extractall(path=temp_extract)

            for m_info in manifest.get("mounts_map", []):
                arc_path = temp_extract / m_info["arcname"]
                dest_src = Path(m_info["source"])
                if arc_path.exists():
                    dest_src.parent.mkdir(parents=True, exist_ok=True)
                    if os.path.isdir(arc_path):
                        if dest_src.exists():
                            shutil.rmtree(dest_src, ignore_errors=True)
                        shutil.copytree(arc_path, dest_src, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copy2(arc_path, dest_src)
        finally:
            shutil.rmtree(temp_extract, ignore_errors=True)
            if manifest.get("encrypted") and tar_to_extract.exists():
                tar_to_extract.unlink()

        src_label = "Cloud" if source == "cloud" else "SSD"
        if existing_container and not recreate_container:
            existing_container.start()
            msg = f"Đã phục hồi [{app_name}] từ {src_label} (bản {version_id}) thành công!"
            update_task(False, "restore", "Khôi phục hoàn tất!", msg, 100, "completed")
            return {"status": "success", "message": msg}

        if existing_container:
            existing_container.remove(force=True)

        recreate_container_from_spec(client, app_name, spec)
        msg = f"Đã tái tạo & phục hồi [{app_name}] từ {src_label} (bản {version_id}) thành công!"
        update_task(False, "restore", "Khôi phục hoàn tất!", msg, 100, "completed")
        return {"status": "success", "message": msg}

    except TaskCancelledError as ce:
        update_task(False, "restore", "Đã hủy khôi phục", str(ce), 100, "cancelled")
        raise HTTPException(status_code=400, detail=str(ce))
    except HTTPException as he:
        update_task(False, "restore", "Lỗi khôi phục", he.detail, 100, "error")
        raise
    except Exception as e:
        update_task(False, "restore", "Lỗi khôi phục", str(e), 100, "error")
        raise HTTPException(status_code=400, detail=f"Lỗi khi khôi phục {app_name}: {str(e)}")

scheduler = BackgroundScheduler()
scheduler.start()

def scheduled_backup_all():
    CANCEL_EVENT.clear()
    run_backup_task("AUTO")

def scheduled_backup_ha():
    CANCEL_EVENT.clear()
    run_backup_task("homeassistant")

def reload_scheduler():
    scheduler.remove_all_jobs()
    settings = load_settings()
    if settings.get("schedule_enabled") and settings.get("schedule"):
        try:
            scheduler.add_job(
                scheduled_backup_all,
                CronTrigger.from_crontab(settings["schedule"]),
                id="auto_backup_all",
                replace_existing=True
            )
        except Exception as e:
            print(f"Lỗi cấu hình Cron: {e}")
    if settings.get("ha_schedule_enabled") and settings.get("ha_schedule"):
        try:
            scheduler.add_job(
                scheduled_backup_ha,
                CronTrigger.from_crontab(settings["ha_schedule"]),
                id="auto_backup_ha",
                replace_existing=True
            )
        except Exception as e:
            print(f"Lỗi cấu hình Cron HA: {e}")

reload_scheduler()

# ================= API ENDPOINTS =================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/task-status")
def api_task_status():
    return TASK_STATE

@app.post("/api/task/cancel")
def api_cancel_task():
    """Hủy ngay lập tức tiến trình đang chạy"""
    CANCEL_EVENT.set()
    with PROC_LOCK:
        if ACTIVE_PROC and ACTIVE_PROC.poll() is None:
            try:
                ACTIVE_PROC.terminate()
            except Exception:
                pass
    update_task(False, TASK_STATE.get("task_type", ""), "Đã hủy tiến trình!", "Người dùng đã dừng tác vụ", 100, "cancelled")
    return {"status": "cancelled"}

@app.get("/api/cloud/apps")
def api_get_cloud_apps():
    """Quét trực tiếp trên Cloud xem có những ứng dụng nào thực sự đang lưu trên Cloud"""
    settings = load_settings()
    remote = settings.get("rclone_remote", "").strip().rstrip("/")
    if not remote or not get_rclone_remotes():
        return {"apps": []}
    try:
        res = subprocess.run(
            ["rclone", "lsf", f"{remote}/apps", "--dirs-only"],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode == 0:
            apps = sorted([line.strip().rstrip("/") for line in res.stdout.splitlines() if line.strip()])
            return {"apps": apps}
    except Exception:
        pass
    # Fallback từ cache nếu mạng chậm
    cache = load_cloud_cache()
    apps = sorted(list({v.get("app_name") for v in cache.values() if isinstance(v, dict) and v.get("app_name")}))
    return {"apps": apps}

@app.get("/api/recovery-key")
def api_export_recovery_key():
    """Tạo Khóa Khôi Phục 1-Chạm (Chứa rclone.conf + settings.json mã hóa Base64) để lưu vào Ghi chú iPhone"""
    rclone_txt = RCLONE_CONFIG_PATH.read_text() if RCLONE_CONFIG_PATH.exists() else ""
    payload = {
        "rclone_conf": rclone_txt,
        "settings": load_settings()
    }
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return {"recovery_key": f"CBMKEY:{b64}"}

@app.post("/api/recovery-key")
async def api_import_recovery_key(request: Request):
    """Khôi phục toàn bộ kết nối Google Drive & Cài đặt chỉ bằng 1 cú dán chuỗi CBMKEY"""
    body = await request.json()
    raw_key = body.get("recovery_key", "").strip()
    if raw_key.startswith("CBMKEY:"):
        raw_key = raw_key[len("CBMKEY:"):]
    try:
        decoded = json.loads(base64.b64decode(raw_key.encode("utf-8")).decode("utf-8"))
        if "rclone_conf" in decoded and decoded["rclone_conf"]:
            RCLONE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            RCLONE_CONFIG_PATH.write_text(decoded["rclone_conf"].strip() + "\n")
        if "settings" in decoded and isinstance(decoded["settings"], dict):
            current = load_settings()
            current.update(decoded["settings"])
            save_settings(current)
            reload_scheduler()
        refresh_cloud_cache()
        return {"status": "restored", "remotes": get_rclone_remotes()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Khóa khôi phục không hợp lệ: {str(e)}")

@app.get("/api/overview")
def api_overview(refresh_cloud: bool = False):
    if refresh_cloud:
        refresh_cloud_cache()
    total, used, free = shutil.disk_usage("/DATA")
    merged_backups = get_merged_backups()
    containers = discover_containers(merged_backups)
    settings = load_settings()

    # Phân tách rõ: Danh sách app đang có bản lưu trên SSD vs Danh sách app đang có bản lưu trên Cloud
    ssd_apps = sorted(list({b["app_name"] for b in merged_backups if b.get("on_ssd")}))
    cloud_apps = sorted(list({b["app_name"] for b in merged_backups if b.get("on_cloud")}))
    all_app_names = sorted(list({c["name"] for c in containers} | {b["app_name"] for b in merged_backups}))

    rclone_conf_content = ""
    if RCLONE_CONFIG_PATH.exists():
        try:
            rclone_conf_content = RCLONE_CONFIG_PATH.read_text()
        except Exception:
            pass

    return {
        "disk": {
            "total_gb": round(total / (1024**3), 1),
            "used_gb": round(used / (1024**3), 1),
            "free_gb": round(free / (1024**3), 1),
            "percent": round((used / total) * 100, 1),
            "backup_size_mb": get_dir_size_mb(BACKUP_ROOT),
            "appdata_size_mb": get_dir_size_mb(Path("/DATA/AppData"))
        },
        "containers": containers,
        "app_names": all_app_names,
        "ssd_apps": ssd_apps,
        "cloud_apps": cloud_apps,
        "backups": merged_backups,
        "settings": settings,
        "rclone": (lambda r: {
            "configured": len(r) > 0,
            "remotes": r,
            "config_text": rclone_conf_content
        })(get_rclone_remotes()),
        "task": TASK_STATE
    }

class BatchBackupRequest(BaseModel):
    app_names: list[str]

@app.post("/api/backup-batch")
def api_backup_batch(req: BatchBackupRequest):
    if TASK_STATE.get("active"):
        raise HTTPException(status_code=400, detail="Hệ thống đang chạy một tiến trình khác!")
    if not req.app_names:
        raise HTTPException(status_code=400, detail="Chưa chọn ứng dụng nào!")
    CANCEL_EVENT.clear()
    update_task(True, "backup", f"Khởi tạo sao lưu ({len(req.app_names)} ứng dụng)...", ", ".join(req.app_names), 5, "running")
    t = threading.Thread(target=run_backup_task, args=(req.app_names,), daemon=True)
    t.start()
    return {"status": "started"}

@app.post("/api/backup/{app_name}")
def api_backup_app(app_name: str):
    if TASK_STATE.get("active"):
        raise HTTPException(status_code=400, detail="Hệ thống đang chạy một tiến trình khác!")
    CANCEL_EVENT.clear()
    update_task(True, "backup", f"Khởi tạo sao lưu ({app_name})...", "Đang chuẩn bị...", 5, "running")
    t = threading.Thread(target=run_backup_task, args=(app_name,), daemon=True)
    t.start()
    return {"status": "started"}

class RestoreRequest(BaseModel):
    app_name: str
    version_id: str
    source: str = "ssd"
    recreate_container: bool = False

@app.post("/api/restore")
def api_restore(req: RestoreRequest):
    return restore_single_app(req.app_name, req.version_id, req.source, req.recreate_container)

@app.delete("/api/backup/{app_name}/{version_id}")
def api_delete_backup(app_name: str, version_id: str, target: str = "both"):
    local_dir = APPS_BACKUP_DIR / app_name / version_id
    if target in ["ssd", "both"] and local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)

    if target in ["cloud", "both"]:
        settings = load_settings()
        remote = settings.get("rclone_remote", "").strip().rstrip("/")
        if remote and get_rclone_remotes():
            subprocess.run(
                ["rclone", "purge", f"{remote}/apps/{app_name}/{version_id}"],
                capture_output=True, text=True, timeout=60
            )
        cache = load_cloud_cache()
        cache.pop(f"{app_name}/{version_id}", None)
        save_cloud_cache(cache)

    return {"status": "deleted", "target": target}

@app.post("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    settings = load_settings()
    settings.update(body)
    save_settings(settings)
    reload_scheduler()
    return {"status": "saved", "settings": settings}

@app.post("/api/rclone/config")
async def api_save_rclone_config(request: Request):
    body = await request.json()
    config_text = body.get("config_text", "").strip()
    RCLONE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RCLONE_CONFIG_PATH.write_text(config_text + "\n")
    remotes = get_rclone_remotes()
    return {"status": "saved", "remotes": remotes}

class CloudActionRequest(BaseModel):
    app_name: str = "ALL"

@app.post("/api/cloud/sync-up")
def api_cloud_sync_up(req: CloudActionRequest = CloudActionRequest()):
    if TASK_STATE.get("active"):
        raise HTTPException(status_code=400, detail="Hệ thống đang bận!")
    CANCEL_EVENT.clear()
    update_task(True, "cloud_sync", f"Đang kết nối Đám mây ({req.app_name})...", "Chuẩn bị đẩy dữ liệu...", 10, "running")
    t = threading.Thread(target=sync_to_cloud, kwargs={"app_name": req.app_name, "update_progress": True}, daemon=True)
    t.start()
    return {"status": "started"}

@app.post("/api/cloud/pull-catalog")
def api_cloud_pull(req: CloudActionRequest = CloudActionRequest()):
    if TASK_STATE.get("active"):
        raise HTTPException(status_code=400, detail="Hệ thống đang bận!")
    settings = load_settings()
    remote = settings.get("rclone_remote", "").strip()
    if not remote or not get_rclone_remotes():
        raise HTTPException(status_code=400, detail="Chưa cấu hình kết nối Cloud (rclone.conf)")
    CANCEL_EVENT.clear()
    update_task(True, "cloud_pull", f"Khởi tạo tải từ Cloud ({req.app_name})...", "Đang kết nối...", 10, "running")
    t = threading.Thread(target=pull_from_cloud_worker, kwargs={"app_name": req.app_name}, daemon=True)
    t.start()
    return {"status": "started"}
