# AGENTS.md — WithLangGraph 项目工作规则



> 本项目是 langTrack 数据链路（weiCheckApp 客户端 + 本仓库服务端）的**服务端/数据侧**。

> 与客户端仓库 `weiCheckApp` 配合：客户端负责采集上报，本仓库负责接收存储、ETL 加工、分析展示。



## 项目定位（必须遵守）



- **纯自用工具**：weiCheckApp 是手机行为数据采集器，不考虑隐私合规。所有数据（剪贴板/短信/屏幕内容/录音）都完整采集、完整保留、不做脱敏。

- **客户端=采集**：weiCheckApp 只负责采集 + 上报，**不做客户端分析**。

- **服务端=加工分析**：本仓库（WithLangGraph）负责 `/ingest` 接收、ETL 清洗建表、report 分析、geocode 逆编码、dashboard 展示。分析能力逐步建设。
  
  

## 终极目标（北极星）

本项目的终极目标**不是"存数据"**，而是 **刻画人物画像、打造一个完全懂主人的助手**。

- 所有采集 / ETL / 分析能力都服务于这一终极目标：理解用户的行为习惯、偏好、生活节奏，而非单纯落库。
- 因此 langTrack 分析增强的优先级为 **C1（行为模式 / 人物画像）> C2（数据质量可视化，保障画像可信）> C3（可行动洞察，画像的即时反馈）**。**C1 是最高优先级**。
- 任何新功能若不能增进"理解主人"，需重新审视其必要性。

## 铁律：

### 改完必更路书

**`docs/langTrack-roadmap.md` 是本项目的数据链路路书（指南 + 工作日志）。**

**每次改动代码（客户端或服务端，哪怕一行）后，必须：**



1. 在路书「执行记录」节追加一条记录：改了什么、为什么改、验证结果

2. 更新「待办」清单：完成项打 ✅，新增项追加

3. 数据相关的验证结论（实测数字、问题诊断）也要记录
   
   

**违反此规则 = 工作不完整，不允许直接收尾。

## 同步文档规则

每次改动代码（哪even 一行）后，必须同步更新如下文件，确保技术文档与代码实现保持一致：

1. `docs/langTrack-roadmap.md`——执行记录与进度节点（如 A①/B/C1 节点），缺一不可。
2. `docs/langTrack-tech.md`——技术实现参考（接口/实体/表字段/数据流图），确保技术细节与代码保持一致，符合 C1>C2>C3 的优先级。

违反此规则会导致技术文档与代码产生偏差，后续维障和复现工作会受阻。**



### 生成md文档规则：



- 结合代码讲细节，不要泛泛而谈
  
  

- 多画图讲数据流程，使用mermaid
  
  

### 数据库表时间字段规范

- **每张表都必须有 `created_at` 与 `updated_at` 两列**，用东八区时间（如 `datetime('now','+8 hours')`），方便人看。
- 新建表一律带这两列；既有表若缺失，在下次结构变更时补上（事实表随 ETL 重建可顺带加）。
- 语义：`created_at`=首次写入时间，`updated_at`=最近一次更新时间。

## 提交流程（沿用既有军规）



1. 提交前 `codegraph sync` 同步索引

2. `git add` 只 stage 本次相关文件

3. commit 用中文描述，格式 `type(scope): 描述`

4. 涉及数据链路的行为变更（采集格式/上报协议/ETL 逻辑）必须先过一遍路书，确认记录同步更新
   
   

## 实测踩坑记录（低级错误警示，避免重犯）



### 0. 脚本文件禁止中文（2026-08-19）

- **规则**：`.ps1` / `.bat` / `.sh` 等**可执行脚本一律纯 ASCII/英文**（注释、输出、报错全英文）

- **原因**：Windows PowerShell 5.1 解析**无 BOM 的 UTF-8 ps1** 时按系统 ANSI(GBK) 读，中文会触发解析错误；而 write/edit 工具写文件**会丢 BOM**，导致"加 BOM → 工具一改又丢"反复踩坑

- **结论**：不依赖 BOM，脚本直接纯 ASCII——最稳；中文只出现在 `.md` 文档和代码注释里

- **连带**：`Write-Host` 输出也写英文；如需中文提示，放文档说明
  
  

### 1. 音频采集线程静默卡死（2026-08-18）



- **现象**：audio_env/audio_clip 数据在某时刻集体停止，全天只剩 34 分钟数据，且**无任何日志**

- **根因**：`AudioRecord.read()` 在系统回收麦克风（打电话/语音）后永久阻塞不返回，采集线程空转

- **错误**：外层 `try-catch(Exception)` 把异常全吞掉，线程死了看不见

- **修复**：read 加超时保护 + 看门狗线程（N 分钟无产出自动重启）+ 保留 Log 日志

- **教训**：
  
  - **后台采集线程必须有超时保护**，凡是 `read()/阻塞 IO` 都要设 deadline
  
  - **吞异常 = 事故**。采集类代码异常必须 `Log.w/e`，不能静默
  
  - **数据突然变少 = 先查采集器存活**，用时间分布（按小时分组）定位停止时刻，别只看总量
    
    

### 2. 服务端 events 表字段名（2026-08-17）



- events 表存的是 `payload` 列（不是 `data`），写 SQL 分析时用错字段会 `no such column`

- 查询前先 `PRAGMA table_info(events)` 或看 `storage.py` 的 `_SCHEMA`
  
  

### 3. Windows 环境编码（2026-08-17）



- `.env` 可能是 GBK 编码，Python `read_text(encoding="utf-8")` 会崩

- 读配置用**字节查找**（`find(b"KEY=")`）避免编码问题

- PowerShell 传中文给 Python 会乱码：用 `python -X utf8` + 写脚本文件而非内联
  
  

### 4. ETL 全量重建会丢人工数据（2026-08-18）



- places 表被 `DELETE + INSERT` 重建后，手工标好的「家/公司」标签全丢

- **教训**：用户人工确认的数据必须持久化到独立配置（`data/place_labels.json`），ETL 重跑后自动恢复

- 通用原则：**ETL 能全量重建的是"可计算数据"，"人工/外部数据"要单独存**
  
  

### 5. Windows 命令行 / 脚本避坑（2026-08-18 汇总）



> 以下坑都是实踩过、反复出现的，**每次写命令/脚本前先扫一眼**。



1. **PowerShell 5.1 不支持 `&&` 连接符**（报 `标记"&&"不是此版本中的有效语句分隔符`）
   
   - 用 `;` 或换行分隔；要拿到命令退出码再判断，用 `$LASTEXITCODE`

2. **`.bat` 必须 CRLF 换行 + GBK 编码**（中文系统代码页 936）
   
   - 写成 LF/UTF-8 时 cmd 解析直接报 `The system cannot find the path specified`
   
   - 用 write_file 写 bat 后务必转换：`(Get-Content x.bat) -join "`r`n" | Set-Content x.bat -Encoding Default`

3. **cmd 下 `\"` 转义无效**（引号问题绕不开时）
   
   - 改用 `powershell -EncodedCommand`：先算 Base64 of UTF-16LE，彻底规避引号地狱

4. **`timeout /t N` 在非交互/重定向时报 `Input redirection is not supported`**
   
   - 改用 `ping -n N 127.0.0.1 >nul` 当延时器

5. **后台常驻进程不要前台拉起**（会被超时杀掉）
   
   - 用 `Start-Process -WindowStyle Hidden` + `cmd /c` 包装（子进程内先 `set PYTHONPATH=src`）+ 输出重定向落盘

6. **只 bind 未 listen 的 socket，psutil.net_connections / netstat 都查不到**
   
   - 查端口占用改用 `Get-NetTCPConnection`；单实例互斥杀旧实例改用**按进程 cmdline 匹配**（如含 `start.py`），避开同名端口服务

7. **git commit 中文 message**（PowerShell 下 `-m "中文(括号)"` 必乱码）
   
   - `python -c` 写 UTF-8 临时文件到 `.git/` 下 + `git commit -F`（相对路径）；`-F` 用长绝对路径会挂起

8. **`.env` 混入 GBK 中文行** → `load_dotenv()` 抛 `UnicodeDecodeError`，进程重启即全挂
   
   - 修：定位坏行（`Get-NetTCPConnection` 找不到就按进程找），坏行转 UTF-8

9. **PowerShell 管道/工具输出被 ANSI 吞或只显示一行**
   
   - 重定向到临时文件再读；GBK 内容用 python 按 `gbk` 解码读取

10. **`2>/dev/null` 是 bash 语法，PowerShell 直接报错**（`无法找到路径 D:\dev\null`）
    
    - 用 `2>$null`；git 输出过滤用 `2>&1` + `Select-String`

11. **write/edit 工具写文件一律 UTF-8 无 BOM**，会覆盖/丢弃已加的 BOM
    
    - 需要 BOM 的场景（ps1 中文）→ 别依赖 BOM，脚本直接纯 ASCII（见第 0 节）

12. **Start-Job 测试后子进程会残留**（Stop-Job 只停主 job）
    
    - 测试完按 cmdline 杀子进程（`Get-CimInstance` + `Stop-Process`）+ 清端口

13. **fastapi TestClient 的 deprecation warning 会让 PowerShell 报 exit code 1**，但测试实际 PASS
    
    - 别被 `[stderr]` + exit 1 误导，看断言结果（`assert PASS` 才算数）

14. **服务/后台进程必须用正确的解释器**（本项目双环境：langTrack 用 `.venv` 有 uvicorn，gacore 用 py12）
    
    - 启动前先确认目标进程的依赖装在哪个环境（`python -c "import uvicorn"`）
      
      

## 常用命令速查



```powershell

# 服务端测试（py12 环境）

$env:PYTHONPATH = "src"

& "D:\softwares\miniconda\envs\py12\python.exe" -m pytest tests/test_langTrack_*.py -q



# ETL + 清理 + 报告

python -m gacore.langTrack.etl --purge

python -m gacore.langTrack.report --day 2026-08-18



# 高德逆编码 / 标签确认

python -m gacore.langTrack.geocode

python -m gacore.langTrack.label_places



# 客户端构建安装（weiCheckApp 仓库）

gradlew.bat :app:assembleDebug --offline

adb install -r app\build\outputs\apk\debug\app-debug.apk

```
