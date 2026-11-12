#!/usr/bin/env python3
"""
配合陶瓦(Terracotta)动态端口：启动陶瓦后，用 psutil 扫描刚出现的本地 LISTEN 端口，
找出那个"陌生的"新端口并打印出来，方便针对性接管。

使用:
    python terracotta_port_scan.py --run "java -jar terracotta.jar"

依赖:
    pip install psutil
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import os
import sys

# 【魔法代码】强制 PyInstaller 把临时文件解压到当前程序所在目录
if getattr(sys, "frozen", False):
    # 把 .exe 所在目录设置为解压目录
    os.environ["_MEIPASS2"] = os.path.dirname(sys.executable)
try:
    import psutil
except ImportError:
    sys.exit("缺少依赖: 请先执行  pip install psutil")


def list_local_listen_ports():
    """返回当前系统所有本地 TCP LISTEN 端口(去重)的集合。"""
    ports = set()
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == "LISTEN":
            # laddr: (ip, port)；取本地端口
            ports.add(conn.laddr.port)
    return ports


def is_port_usable(port):
    """检查一个端口当前是否可被绑定(用来排除已被本脚本/其它占用的口)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    # ================= 自动提权机制 =================
    import ctypes
    import sys
    import os
    
    # 如果当前不是管理员，立刻用 runas 重新拉起自己（会弹 UAC 确认框）
    if not ctypes.windll.shell32.IsUserAnAdmin():
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, " ".join(sys.argv), None, 1
            )
        except Exception:
            pass
        sys.exit(0)
    # ================================================
    
    # ... 下面是你原本的找 terracotta.exe、扫端口、拉起网页的代码 ...
    # ================================================
    
    # 下面是你原来的逻辑...
    # ================= 防呆机制 =================
    import ctypes
    import psutil
    try:
        # 如果父进程是资源管理器(explorer.exe)，说明用户是直接双击运行的
        parent = psutil.Process(os.getpid()).parent()
        parent_name = parent.name().lower() if parent else ""
        if parent_name == "explorer.exe":
            ctypes.windll.user32.MessageBoxW(
                0, 
                "请勿单独运行此程序！\n\n请通过 PyMC 启动器的「陶瓦联机」页面启动。\n如果直接运行，会导致陶瓦进程无法被启动器接管和关闭。", 
                "PyMC-Launcher 提示", 
                0x30  # 警告图标
            )
            sys.exit(0)
    except Exception:
        pass

    # ================= 自动寻找陶瓦 =================
    # 默认假设 terracotta.exe 和这个 exe 在同级目录
    base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    
    # 找 terracotta.exe (兼容带版本号的命名)
    exe_path = None
    for f in os.listdir(base_dir):
        if f.lower().startswith("terracotta") and f.lower().endswith(".exe"):
            exe_path = os.path.join(base_dir, f)
            break
            
    if not exe_path:
        ctypes.windll.user32.MessageBoxW(0, "未找到 terracotta.exe！\n请确保它和启动器在同一目录下。", "错误", 0x10)
        sys.exit(1)

    # ================= 执行逻辑 =================
    base_ports = list_local_listen_ports()
    
    # 启动陶瓦：关键注入 BROWSER=echo 拦截网页！
    env = os.environ.copy()
    env["BROWSER"] = "echo" 
    
    try:
        proc = subprocess.Popen(
            [exe_path],
            cwd=base_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        sys.exit(1)

    # 扫描端口
    start = time.time()
    found = None
    while time.time() - start < 60.0:  # 60秒超时
        ret = proc.poll()
        if ret is not None:
            sys.exit(1) # 陶瓦提前退出了

        now_ports = list_local_listen_ports()
        new_ports = now_ports - base_ports
        candidates = {p for p in new_ports if not is_port_usable(p)}
        if candidates:
            found = sorted(candidates)[0]
            break

        time.sleep(0.3)

    # 拉起网页
    if found:
        url = f"http://127.0.0.1:{found}/"
        try:
            # 这里用 cmd start 降权拉起，绝对不闪退
            a=0
            #subprocess.Popen(f'cmd /c start "" "{url}"', shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    else:
        # 没找到端口，尝试直接杀掉陶瓦
        try: proc.terminate()
        except: pass
        sys.exit(2)

    # 挂起等待：只要这个 Python 脚本活着，陶瓦就活着
    # 启动器杀掉这个 Python 进程时，会触发这里的 finally
    try:
        while proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            # 脚本退出时，连根拔起陶瓦，防止留下孤儿进程
            parent_p = psutil.Process(proc.pid)
            for child in parent_p.children(recursive=True):
                child.kill()
            parent_p.kill()
        except Exception:
            try: proc.terminate()
            except: pass

if __name__ == "__main__":
    main()



