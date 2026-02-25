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
  │ XSetSelectionOwner() │     │  ring-buffer callback        │
  │         ↓            │     │      ↓ (non-blocking put)    │
  │ PT_REGS_PARM2 过滤   │────>│  Queue(1) ──> sync_worker   │
  │ selection==XA_PRIMARY│     │                 ↓            │
  │ ringbuf_submit()     │     │    xclip / xsel / wl-copy   │
  └─────────────────────┘     └──────────────────────────────┘
```

**数据流：**

1. 用户选中文本 → 应用调用 `XSetSelectionOwner(display, XA_PRIMARY, ...)`
2. eBPF uprobe 在内核态通过 `PT_REGS_PARM2` 读取第二个参数，**仅放行 `XA_PRIMARY`**
3. 事件经 ring-buffer 上报到用户态回调
4. 回调以 `put_nowait` 向固定大小为 1 的 Queue 发信号（**非阻塞，纳秒级**）
5. 独立 daemon 线程 `_sync_worker` 消费信号，调用 subprocess 执行 PRIMARY → CLIPBOARD 同步
6. 消费端 debounce（0.05s sleep）自动合并突发事件

## 主要模块说明

### eBPF Uprobe（内核态过滤）

- 挂载到 `libX11.so.6:XSetSelectionOwner()`
- 通过 `PT_REGS_PARM2(ctx)` 获取 `Atom selection` 参数
- `selection != XA_PRIMARY` 直接 `return 0`
- 仅 PRIMARY 变化才触发 `ringbuf_submit`

### eBPF 内核态过滤器

- `is_sync_tool()` 过滤 xclip / xsel / wl-copy 的反馈循环
- 通过逐字节比较进程名实现

### 生产/消费解耦

- 回调函数不执行 subprocess，仅往 `queue.Queue(maxsize=1)` 放信号
- `_sync_worker` daemon 线程循环消费
- 队列满时 `put_nowait` 自动合并事件

### 剪贴板同步

- 优先尝试 `xclip`，其次 `xsel`，最后 `wl-clipboard`
- subprocess 调用设置 2s timeout 并使用 `start_new_session=True` 隔离进程

### libX11 路径发现

解析优先级：

1. `ctypes.util.find_library("X11")`
2. `ldconfig -p` 显式解析
3. **`/proc/*/maps` 扫描**（适配 Snap/Flatpak/自定义路径）
4. 硬编码 multiarch 路径

### 自动修复 XAUTHORITY

- `sudo` 环境下自动定位 `~/.Xauthority` 或 `/run/user/<uid>/xauth_*`
- 确保 `xclip` 在 service 环境下正常工作

### 资源清理

- `run()` 使用 `finally: b.cleanup()` 确保 uprobe detach

## 依赖

- **Linux kernel >= 5.8**（Ring-buffer 支持）
- `python3-bpfcc`
- `libx11-6`
- `xclip` 或 `xsel`
- `wl-clipboard`（可选）

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
