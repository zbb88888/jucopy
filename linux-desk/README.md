# jucopy

> eBPF-driven clipboard sync for Linux desktops — select text, paste with Ctrl+V.

## 工程目标

自动将 Linux 桌面环境下的 **PRIMARY 选区**（鼠标选中文本）同步到
**CLIPBOARD**（Ctrl+V 粘贴缓冲区），实现"选即复制"体验。
通过 eBPF uprobe 监控 libX11 的 `XSetSelectionOwner()`，实时捕获选区变更事件。

## 架构

```text
  eBPF uprobe (kernel)              User-space (Python)
  ┌─────────────────────┐     ┌──────────────────────────────┐
  │ XSetSelectionOwner() │     │  perf-buffer callback        │
  │         ↓            │     │      ↓ (non-blocking put)    │
  │ PT_REGS_PARM2 过滤   │────>│  Queue(1) ──> sync_worker   │
  │ selection==XA_PRIMARY│     │                 ↓            │
  │ perf_submit()        │     │    xclip / xsel / wl-copy   │
  └─────────────────────┘     └──────────────────────────────┘
```

**数据流：**

1. 用户选中文本 → 应用调用 `XSetSelectionOwner(display, XA_PRIMARY, ...)`
2. eBPF uprobe 在内核态通过 `PT_REGS_PARM2` 读取第二个参数，**仅放行 `XA_PRIMARY`**
3. 事件经 perf-buffer 上报到用户态回调
4. 回调以 `put_nowait` 向固定大小为 1 的 Queue 发信号（**非阻塞，纳秒级**）
5. 独立 daemon 线程 `_sync_worker` 消费信号，调用 subprocess 执行 PRIMARY → CLIPBOARD 同步
6. 消费端 debounce（0.05s sleep）自动合并突发事件

## 主要模块说明

### eBPF Uprobe（内核态过滤）

- 挂载到 `libX11.so.6:XSetSelectionOwner()`
- 通过 `PT_REGS_PARM2(ctx)` 获取 `Atom selection` 参数
- `selection != XA_PRIMARY` 直接 `return 0`，避免无意义的 context switch
- 仅 PRIMARY 变化才触发 `perf_submit`（包含 PID、进程名）

### eBPF 内核态过滤器

- 新增 `is_sync_tool()` 内联函数，过滤 xclip / xsel / wl-copy / wl-paste 的反馈循环
- 通过逐字节比较进程名实现，兼容内核版本 ≥ 4.14

### 生产/消费解耦

- 回调函数**不执行 subprocess**，仅往 `queue.Queue(maxsize=1)` 放信号
- `_sync_worker` daemon 线程循环消费，执行实际的剪贴板同步
- 队列满时 `put_nowait` 抛出 `queue.Full`，静默丢弃（coalesce）
- 消费端 sleep debounce 合并连续事件

### 剪贴板同步

- 优先尝试 `xclip`，其次 `xsel`，最后 `wl-clipboard`（Wayland 环境）
- subprocess 调用设置 2s timeout，异常自动忽略，保证守护进程稳定运行

### libX11 路径发现

解析优先级：

1. `ctypes.util.find_library("X11")` — 标准库方法，覆盖 `ld.so.conf` 搜索路径
2. 硬编码 multiarch glob 模式 — x86\_64 / aarch64 / 通配
3. `ldconfig -p` 显式解析 — 最终 fallback

### DISPLAY 环境检查

- `sudo` 环境下 `DISPLAY` 经常为空，在 `main()` 入口显式检查并给出修复建议
- 支持 `--display` 参数手动指定

### 自动修复 XAUTHORITY

- `sudo` 环境下自动定位 `~/.Xauthority` 或 `/run/user/<uid>/xauth_*`
- 确保 `xclip` 在无 DISPLAY 的情况下正常工作

### libX11 动态解析

- 优先通过 `/proc/*/maps` 扫描运行中的 X11 进程，获取真实路径
- 兼容非标准安装路径（如 Flatpak、Snap、自定义 LD_LIBRARY_PATH）

### 资源清理

- `run()` 的主循环包裹在 `try/finally` 中，退出时调用 `b.cleanup()`
- 确保 uprobe 被正确 detach，避免残留探针

### 权限校验

- eBPF 需要 `CAP_SYS_ADMIN`（root），启动前校验并提示

### 可观测性

- `--verbose` 输出每次 selection event 的 PID、comm 和同步结果
- 便于生产环境 debug 和 systemd journal 采集

## 控制流

```text
main()
  ├── argparse: --display, --verbose
  ├── check_root()
  ├── check_display()         # DISPLAY 为空时警告
  ├── find_libx11()           # ctypes → glob → ldconfig
  └── run()
        ├── BPF(text=BPF_TEXT)
        ├── attach_uprobe(XSetSelectionOwner)
        ├── start _sync_worker (daemon thread)
        ├── open_perf_buffer(handle_event)
        ├── loop: perf_buffer_poll()
        └── finally: b.cleanup()
```

## 生产级特性

| 特性 | 实现 |
| ---- | ---- |
| 幂等性 | 内核态过滤 + 消费端 debounce，每次选区只同步一次 |
| 高性能 | 回调零 subprocess 开销，异步 worker 线程执行 I/O |
| 高可用 | 缺少剪贴板工具不会导致守护进程崩溃 |
| 资源安全 | `finally: b.cleanup()` 确保 uprobe detach |
| 可观测性 | `--verbose` 详细事件日志，适配 systemd journal |

## 依赖

- Linux kernel >= 4.14（eBPF 支持）
- `python3-bpfcc`（BCC Python bindings）
- `libx11-6`（libX11.so.6）
- `xclip` 或 `xsel`（X11 / XWayland）
- `wl-clipboard`（Wayland 原生，可选）

## 安装

```bash
sudo bash install.sh
```

## 使用

```bash
# 直接运行
sudo python3 jucopy.py

# 指定 DISPLAY 并开启详细日志
sudo python3 jucopy.py --display :0 --verbose

# systemd 服务
sudo systemctl enable --now jucopy
```

## 已知限制

- 纯 Wayland session（非 XWayland）下 `libX11` 的 uprobe 不会触发，
  需针对合成器（`gnome-shell` / `kwin` / `wlroots`）做额外 hook
- Headless 节点（无 DISPLAY）上运行会因找不到 `libX11` 或 `DISPLAY` 而快速失败
