代码设计核心如下：

---

**工程目标**  
自动将 Linux 桌面环境下的 PRIMARY 选区（鼠标选中文本）同步到 CLIPBOARD（Ctrl+V 粘贴缓冲区），实现“选即复制”体验。通过 eBPF uprobe 监控 libX11 的 XSetSelectionOwner()，实时捕获选区变更事件。

---

**架构流程**

```
用户选中文本
   ↓
libX11.so.6:XSetSelectionOwner() 被调用
   ↓
eBPF uprobe (BCC) 捕获 → 事件上报
   ↓
Python 守护进程监听 perf buffer
   ↓
sync_selection() 执行 PRIMARY → CLIPBOARD 同步
   ↓
调用 xclip/xsel/wl-clipboard 工具
```

---

**主要模块说明**

- **eBPF Uprobe**  
  - 动态挂载到 libX11 的 XSetSelectionOwner()，每次选区变更时触发事件（包含 PID、进程名）。
  - 事件通过 perf buffer 传递到 Python 层。

- **去重/防抖**  
  - 0.05 秒内重复事件自动忽略，防止多次同步。

- **剪贴板同步**  
  - 优先尝试 xclip，其次 xsel，最后 wl-clipboard（Wayland 环境）。
  - subprocess 调用，异常自动忽略，保证守护进程稳定运行。

- **libX11 路径发现**  
  - 先查常见路径，找不到则用 ldconfig -p 查询。

- **权限校验**  
  - eBPF 需要 root 权限（CAP_SYS_ADMIN），启动前校验。

- **可观测性**  
  - --verbose 参数可输出详细事件和同步日志，便于调试。

- **容错与健壮性**  
  - 缺少依赖、库、BCC 等均有明确错误提示并安全退出。

---

**生产级特性**

- **幂等性**  
  - 防抖机制确保每次选区只同步一次。

- **安全性**  
  - eBPF 需 root，剪贴板同步部分以用户权限执行。

- **高可用**  
  - 缺少剪贴板工具不会导致守护进程崩溃。

- **日志与调试**  
  - 详细事件输出，便于定位问题。

---

**控制流简述**

- `main()`：解析参数、校验权限、查找 libX11、启动 run()
- `run()`：挂载 eBPF uprobe，注册事件处理，循环监听 perf buffer
- `handle_event()`：防抖、日志输出、调用 sync_selection()
- `sync_selection()`：依次尝试剪贴板工具，输出同步结果

---

此设计充分利用 eBPF 实现高效、实时的桌面剪贴板同步，兼顾容错、幂等、可观测性，适合生产环境部署。