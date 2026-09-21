"""
Bonsai 2 27B Llama-Server Runner & Daemon Manager for Windows.
Handles detached process management, log file streaming, PID tracking, and graceful shutdown.
"""
import os
import sys
import time
import json
import signal
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = PROJECT_ROOT / "infra" / "bonsai" / "config.env"
LOGS_DIR = PROJECT_ROOT / "logs"
PID_FILE = LOGS_DIR / "bonsai_server.pid"
LOG_FILE = LOGS_DIR / "bonsai_server.log"

def load_env_config():
    config = {
        "BONSAI_MODEL_PATH": r"D:\RJ\models\bonsai2\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        "BONSAI_BIN_DIR": str(PROJECT_ROOT / "infra" / "bonsai" / "bin"),
        "BONSAI_HOST": "127.0.0.1",
        "BONSAI_PORT": "8080",
        "BONSAI_CTX": "8192",
        "BONSAI_NGL": "99",
        "BONSAI_KV4": "1",
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    
    # Process env overrides
    for k in config:
        if k in os.environ:
            config[k] = os.environ[k]
    return config

def get_running_pid():
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        # Check if process is actually running
        import psutil
        if psutil.pid_exists(pid):
            p = psutil.Process(pid)
            if "llama-server" in p.name().lower():
                return pid
    except Exception:
        pass
    return None

def stop_server():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    pid = get_running_pid()
    stopped = False
    if pid:
        try:
            import psutil
            p = psutil.Process(pid)
            print(f"[INFO] Terminating llama-server PID {pid}...")
            p.terminate()
            p.wait(timeout=5)
            stopped = True
        except Exception:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                stopped = True
            except Exception:
                pass
    
    # Clean any dangling llama-server processes
    try:
        subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    print("[OK] Bonsai 2 27B server has been stopped.")
    return True

def start_server():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = get_running_pid()
    if existing_pid:
        print(f"[OK] Bonsai 2 27B is already running with PID {existing_pid}.")
        return True

    cfg = load_env_config()
    bin_dir = Path(cfg["BONSAI_BIN_DIR"])
    server_exe = bin_dir / "llama-server.exe"
    model_path = Path(cfg["BONSAI_MODEL_PATH"])

    if not server_exe.exists():
        print(f"[ERR] Executable not found: {server_exe}", file=sys.stderr)
        return False

    if not model_path.exists():
        print(f"[ERR] Model weights not found: {model_path}", file=sys.stderr)
        return False

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir};{env.get('PATH', '')}"

    args = [
        str(server_exe),
        "-m", str(model_path),
        "--host", cfg["BONSAI_HOST"],
        "--port", cfg["BONSAI_PORT"],
        "-c", str(cfg["BONSAI_CTX"]),
        "-ngl", str(cfg["BONSAI_NGL"]),
        "--flash-attn", "on",
    ]

    if str(cfg.get("BONSAI_KV4", "1")) == "1":
        args.extend(["-ctk", "q4_0", "-ctv", "q4_0"])

    print("=========================================")
    print(" Starting PrismML Bonsai 2 27B Daemon")
    print(f" Model:    {model_path}")
    print(f" Host:     {cfg['BONSAI_HOST']}:{cfg['BONSAI_PORT']}")
    print(f" Context:  {cfg['BONSAI_CTX']}")
    print(f" GPU NGL:  {cfg['BONSAI_NGL']}")
    print(f" KV4:      {cfg.get('BONSAI_KV4', '1')}")
    print(f" Log File: {LOG_FILE}")
    print("=========================================")

    # Open log file directly in append mode
    log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

    # Windows flags for truly detached background service
    DETACHED_FLAGS = 0x00000008 | 0x00000200 # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        args,
        env=env,
        cwd=str(bin_dir),
        stdout=log_fp,
        stderr=log_fp,
        creationflags=DETACHED_FLAGS,
        close_fds=True,
    )

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[INFO] Server spawned with PID {proc.pid}. Verifying health...")

    # Health check probe loop
    import urllib.request
    health_url = f"http://{cfg['BONSAI_HOST']}:{cfg['BONSAI_PORT']}/health"
    healthy = False
    for i in range(40):
        time.sleep(2)
        # Check if process is still alive
        if proc.poll() is not None:
            print(f"[ERR] Server terminated early with returncode {proc.returncode}!", file=sys.stderr)
            return False
        try:
            req = urllib.request.Request(health_url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = resp.read().decode("utf-8")
                    if "ok" in data.lower():
                        healthy = True
                        break
        except Exception:
            pass
        print(f"  ... waiting for CUDA model initialization ({i*2}s)")

    if healthy:
        print(f"[SUCCESS] Bonsai 2 27B Server is online at http://{cfg['BONSAI_HOST']}:{cfg['BONSAI_PORT']}/v1")
        return True
    else:
        print(f"[WARN] Server process PID {proc.pid} is alive, but /health timed out.", file=sys.stderr)
        return False

def check_status():
    pid = get_running_pid()
    cfg = load_env_config()
    health_url = f"http://{cfg['BONSAI_HOST']}:{cfg['BONSAI_PORT']}/health"
    status = {"running": False, "pid": pid, "api_url": f"http://{cfg['BONSAI_HOST']}:{cfg['BONSAI_PORT']}/v1"}
    if pid:
        status["running"] = True
        import urllib.request
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                status["health"] = "ok" if resp.status == 200 else "error"
        except Exception:
            status["health"] = "unresponsive"
    print(json.dumps(status, indent=2))
    return status["running"]

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        success = start_server()
        sys.exit(0 if success else 1)
    elif action == "stop":
        stop_server()
        sys.exit(0)
    elif action == "status":
        is_running = check_status()
        sys.exit(0 if is_running else 1)
    else:
        print(f"Unknown action: {action}. Use start, stop, or status.")
        sys.exit(1)
