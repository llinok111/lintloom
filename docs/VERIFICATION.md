# 验证记录 / Verification

本文件记录 **2026-10-23** 在本机（Windows 10/11，Python 3.13.14）对 LintLoom 与上游 sqlfluff 的实测结果。
所有数字与输出均来自真实执行，原始日志在 `docs/evidence/`。

---

## 1. 环境

| 项 | 值 | 来源 |
|---|---|---|
| 操作系统 | win32（Windows 10/11） | 会话环境 |
| Python（项目 venv） | 3.13.14 | `python -V` |
| sqlfluff | 4.4.0 | `python -m sqlfluff --version` → `python -m sqlfluff, version 4.4.0` |
| 浏览器（验收） | Microsoft Edge（Playwright `channel: msedge`，headless） | `docs/evidence/08-ui-verify.txt` |

---

## 2. 上游核实（api.github.com）

```jsonc
// GET /repos/sqlfluff/sqlfluff   2026-10-23
{
  "full_name": "sqlfluff/sqlfluff",
  "stargazers_count": 9989,
  "forks_count": 1257,
  "license": { "key": "mit", "spdx_id": "MIT" },
  "default_branch": "main",
  "pushed_at": "2026-10-21T15:30:21Z",
  "archived": false,
  "language": "Python"
}
// GET /repos/sqlfluff/sqlfluff/releases/latest
{ "tag_name": "4.4.0", "published_at": "2026-10-02T18:54:08Z" }
// GET /repos/sqlfluff/sqlfluff/commits?per_page=1
1b62cf0d979ce335ffb16d8ff7cfffe6d31ab021  2026-10-21  Snowflake: Support GIT REPOSITORY statements (#8803)
```

原始 JSON：`docs/evidence/01-github-api.json`、`01b-release.json`。

---

## 3. CLI 行为实测

### 3.1 诊断

```
$ python -m sqlfluff lint samples/messy_orders.sql -d ansi -f json
exit=1
[{"filepath":"samples\\messy_orders.sql","violations":[ ... ],
  "statistics":{"source_chars":218,"templated_chars":218,"segments":176,"raw_segments":124},
  "timings":{"templating":0.0003,"lexing":0.0021,"parsing":0.0356,"linting":0.0201, "LT01":... }}]
```

- 该样例：**14 条违规**，规则码 `AL01 / AM05 / AM06 / LT01 / LT09`。
- JSON 顶层是**数组**（每个文件一项），每项含 `violations / statistics / timings`。
- `timings` 里既有阶段耗时，也有**每条规则**的耗时 —— 界面的「计时」页直接用它，不做二次估算。
- 原始文件：`docs/evidence/03-lint.json`。

### 3.2 修复（`--fixed-suffix` 与 stdin）

```
$ python -m sqlfluff fix .tmp/fixdemo/messy_orders.sql -d ansi -x _fixed
==== finding fixable violations ====
== [.tmp\fixdemo\messy_orders.sql] FIXED
15 fixable linting violations found
  [1 unfixable linting violations found]
exit=1
# 产出：messy_orders.sql（原样）+ messy_orders_fixed.sql（修复结果）
```

```
$ printf 'select a,b from t where x=1' | python -m sqlfluff fix - -d ansi
select
    a,
    b
from t
where x = 1
exit=0
```

结论（决定了本项目的实现方式）：

1. `-x <suffix>` 会写**另一个文件**，原文件不动 → 用它做「预览」最安全。
2. `fix -` 把修复结果写到 **stdout**，不落盘 → 编辑器缓冲区模式直接采用。
3. `fix` 的退出码在「仍有不可修项」时是 `1`，语法错误时是 `2` → 只把 `2` 当失败。

原始文件：`docs/evidence/04-fix-suffix.txt`。

### 3.3 语法树

```
$ python -m sqlfluff parse samples/messy_orders.sql -d ansi -f json
[{"filepath":"samples\\messy_orders.sql","segments":{"file":{"statement":{"select_statement":[...]}}}}]
```

`-f json` 的结构是 `file → statement → select_statement → select_clause …`，
界面的语法树按这个嵌套关系递归渲染（实测 52 组 / 95 叶子节点）。

### 3.4 错误场景（诊断归类的依据）

| 输入 | stderr 关键文本 | 退出码 | 界面归类 |
|---|---|---|---|
| `-d notadialect` | `Error: Unknown dialect 'notadialect'` | 2 | `DIALECT` |
| 不存在的路径 | `User Error: Specified path does not exist...` | 2 | `PATH` |
| `-r ZZ999` | `WARNING Tried to allowlist unknown rule references: ['ZZ999']` | **0** | `RULES`（warn，不阻断） |
| 超过超时 | ——（进程被杀） | — | `TIMEOUT` |
| SQL 无法解析 | violations 里出现 `PRS` | 1 | `PARSE` |

注意最后两行：**未知规则不影响退出码**，所以「静默漏检」是真实风险 —— 本项目因此在
参数校验阶段就用运行时目录做白名单，不让这类输入走到 CLI（见 `app/paths.py::validate_rule_list`）。

原始文件：`docs/evidence/05-errors.txt`。

### 3.5 一个容易踩的坑：方言名被截断

`sqlfluff dialects` 的文本输出按 80 列排版，长名字会被切断：

```
materializ:   Materialize dialect [inherits from 'postgres']
e
```

所以本项目**不把文本输出当权威**：主路径走公开 API `dialect_readout()`（得到 28 个方言，
`materialize` 完好），文本解析只作兜底，且实现了续行合并 + 去掉尾部 `dialect` 词，
并有 4 项单元测试锁住这个行为（`tests/test_catalog_fallback.py`）。

---

## 4. 应用侧验证

### 4.1 环境自检

```
$ python run.py --selftest
{
  "upstream": {"repo":"sqlfluff/sqlfluff","pinned":"4.4.0","version":"4.4.0",
               "cli":"python -m sqlfluff, version 4.4.0"},
  "catalog": {"ok": true, "source": "python-api", "dialects": 28 项, "rules": 79 条},
  "checks": [解释器 OK, sqlfluff CLI OK, 能力目录 OK, 写回开关 OK]
}
```

原始文件：`docs/evidence/06-selftest.json`。

### 4.2 真实作业（HTTP → 子进程 → 结果）

| 动作 | 输入 | 结果 |
|---|---|---|
| `lint`（目录） | `samples/`，ansi | 3 文件 / 23 项违规；`{LT01:10, AL01:4, LT09:3, LT14:3, AM05:1, AM06:1, LT05:1}`；4.5 s |
| `fix_preview` | `samples/warehouse/` | 2 文件改写，`+13 / −2`，修复后复核**剩余 0 项**；源文件 sha256 不变 |
| `fix_apply` | 同上 + 确认 | 写入 2 文件，备份落在 `.lintloom-backups/<时间戳>/samples/…` |
| `parse` | `samples/messy_orders.sql` | `file → statement → select_statement` 树，52 组节点 |
| `lint_text` | `select a,b from t` | 9 项违规（stdin，不落盘） |
| 超时 | 单文件 + `timeout=1s` | 状态 `timeout`，进程树被回收（见 4.3） |

### 4.3 进程树回收（最容易出错的地方）

专门写了一个「孙进程」测试：父进程再 spawn 一个子进程，子进程 3 秒后写 marker 文件。
给父进程 1 秒超时后，等 4.5 秒检查 —— **marker 不存在**，说明孙进程也被杀了：

```
test_process_tree_is_killed ... ok
```

Windows 下用 `taskkill /F /T /PID`，POSIX 用 `killpg(getpgid(pid), SIGKILL)`。

### 4.4 全量测试

```
$ python -m unittest discover -s tests
......................................................................s.............................
Ran 100 tests in 69.306s
OK (skipped=1)
```

- 1 项 skip = symlink 逃逸用例（Windows 非管理员无法建 symlink），
  同一保护逻辑由 `test_realpath_guard_blocks_escape_without_symlink_privileges` 覆盖。
- `test_engine.py` / `test_server.py` **不 mock 上游**，真的起 sqlfluff 进程。
- 原始日志：`docs/evidence/07-unittest.txt`。

---

## 5. 界面验收（真机浏览器）

用 Playwright + Edge 内核真实打开页面、真实点击按钮、真实读 DOM（不靠人眼截图判断）：

```
合计 24 项，通过 24，失败 0
```

覆盖：环境徽标（sqlfluff 4.4.0 / 28 方言 / 79 规则）· 根目录切换 · 文件树 · 目录诊断
（2 文件 / 9 行，规则码 `LT09,AL01,LT14,LT05,LT01`）· 进度条 100% · 点击问题行跳到代码并
**行号一致** · 修复预览 diff（`+13/−2`，附「复核剩余 0 项」）· 语法树（52 组 / 95 叶子）·
计时条 24 条非零宽度 · 日志含真实 argv · stdin 模式 · **写回确认弹窗 → 真实改文件（218→249 字节）
→ 备份落盘** · 无 JS 报错 · 布局尺寸合理且无水平溢出。

原始日志：`docs/evidence/08-ui-verify.txt`。

### 5.1 回归：界面改动后重跑

在把截图接入 README 之前，界面补了一处真实缺口 —— **根目录本身无法整体作为目标**
（原先只有子目录才有「把当前目录整体作为目标」入口，导致在根目录下点「诊断」必须逐个文件选）。
修改 `app/web/app.js::loadTree` 后，重新跑了完整验收：

```
合计 24 项，通过 24，失败 0
文件树有内容 :: 4 项   ← 多出「把整个根目录作为目标」入口
```

结论：改动未引入回归；所有截图与结论均来自改动**之后**的这一轮。

### 5.2 仓库配图（自绘 SVG + 真实截图）

- 界面截图 8 张：由 Playwright + Edge 真实点击产生，存于 `docs/screenshots/`。
- 架构图 / 横幅 2 张：本项目自绘 SVG（`docs/assets/`），**不依赖任何外部图床或设计工具**。
- 两者都做了像素级验证，阈值按各自尺寸设定：

```
文件                    尺寸              色数     亮度  分带标准差   (阈值 1200x300)
architecture.png      1200x560       297   27.9  [28.0, 23.0, 23.9, 25.8]  OK
banner.png            1280x320       425   33.1  [12.0, 43.8, 41.1, 15.1]  OK
共 2 张，可疑 0 张

文件                    尺寸              色数     亮度  分带标准差   (阈值 1600x1000)
01-diagnostics.png    1600x1000      298   26.4  [23.7, 22.8, 22.0, 23.9]  OK
02-fix-preview.png    1600x1000      362   30.8  [24.1, 18.9, 26.1, 23.6]  OK
03-code-jump.png      1600x1000      291   22.8  [24.4, 13.1, 22.0, 23.4]  OK
04-parse-tree.png     1600x1000      280   22.5  [22.9, 12.7, 23.4, 22.4]  OK
05-run-log.png        1600x1000      647   29.1  [27.8, 45.2, 40.4, 22.3]  OK
06-writeback-confirm.png1600x1000      201   15.4  [7.4, 13.5, 13.4, 6.6]  OK
07-writeback-applied.png1600x1000      280   23.9  [25.7, 11.5, 23.4, 22.1]  OK
08-timing.png         1600x1000      282   30.8  [21.7, 14.3, 22.9, 24.5]  OK
共 8 张，可疑 0 张
```

SVG 额外验证了「真的画出来了」：用 Edge 渲染后统计图形元素数
（`architecture.svg` 58 个形状 / 39 段文本，`banner.svg` 35 个形状 / 13 段文本，均 0 报错），
而不是只看文件大小。

### 5.3 原「截图不是空帧」记录（改动前，保留作对照）

由于本机读图工具不可靠，截图另做**自解码像素统计**（zlib + 反滤波），四条阈值同时成立才算通过：
尺寸 ≥ 1200×600、去重颜色 ≥ 200、平均亮度 5–250、四段分带标准差均 > 2。

```
文件                    尺寸              色数     亮度  分带标准差
01-issues.png         1600x1000      296   26.3  [23.7, 22.8, 22.0, 23.9]  OK
03-diff.png           1600x1000      363   30.8  [24.1, 18.9, 26.1, 23.6]  OK
04-parse.png          1600x1000      275   22.4  [23.0, 11.0, 25.2, 21.0]  OK
07-confirm.png        1600x1000      201   15.4  [7.4, 13.5, 13.4, 6.6]  OK
共 11 张，可疑 0 张
```

原始日志：`docs/evidence/09-png-stats.txt`；截图同在 `docs/evidence/`。

---

## 6. 复现方式

```bash
# 1. 环境
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# 2. 自检（应为 exit 0，catalog.ok=true）
.venv/Scripts/python run.py --selftest

# 3. 测试（应为 100 passed / 1 skipped）
.venv/Scripts/python -m unittest discover -s tests -v

# 4. 起服务并用浏览器点一遍
.venv/Scripts/python run.py
```

界面验收脚本不在交付包内（它依赖本机的 Edge + Playwright），但复现路径已在
`docs/evidence/08-ui-verify.txt` 中完整列出每一步的断言与实测值。

---

## 7. 遗留风险（如实记录）

1. **未在 POSIX 实机验证**：`killpg` 分支与 `start_new_session` 只有代码路径，没有实跑证据。
2. **`fix` 报告数与 `lint` 违规数不必相等**：上游 `fix` 会跨多轮迭代统计（本例 15 vs 14），
   界面展示的是上游原话，不做归一化。
3. **`.sqlfluffignore` 未参与目录遍历**：本工具自己 walk 目录以换取逐文件进度，
   被 ignore 的文件仍会列出来。
4. **模板化（Jinja/dbt）依赖项目侧配置**：本工具只把 `TEMPLATE` 类错误归类出来，不生成配置。
5. **单机单用户**：无鉴权分级、无历史持久化；token 只防同机网页，不防同机进程。

---

## 8. ZIP 独立验证（交付物本身可用）

不信任源码树，直接把 ZIP 解压到全新目录，重建虚拟环境、重装依赖、重跑全部测试与界面验收：

```
解压     : _zipverify-lintloom/LintLoom（全新空目录）
venv     : python -m venv .venv（Python 3.13.14）
依赖     : pip install -r requirements.txt（aliyun 镜像，exit=0）
版本     : python -m sqlfluff --version → python -m sqlfluff, version 4.4.0
自检     : python run.py --selftest → exit=0，catalog.ok=true，28 方言 / 79 规则
测试     : Ran 100 tests in 81.213s → OK (skipped=1)
界面验收 : 起服务（--port 8733）→ 24 项全部通过，含「写回真实改文件 + 备份落盘」
```

- 界面验收脚本对它跑出的结果与原目录一致（`docs/evidence/10-zip-verify.txt` 完整记录）。
- 该 ZIP 内**不含** `.venv / __pycache__ / .lintloom-* / .tmp / shots`，因此必须走一次安装；
  这正是本验证要证明的点。

### 复现

```bash
unzip LintLoom-1.0.0.zip && cd LintLoom
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python run.py --selftest
.venv/Scripts/python -m unittest discover -s tests
.venv/Scripts/python run.py
```

---

## 9. 从 GitHub 克隆后的独立验证

不只验证本地源码树，而是 **从远端重新 clone**，在全新目录重建环境再跑一遍：

```
$ git clone git@github-llinok111:llinok111/lintloom.git _clone-lintloom
$ cd _clone-lintloom && git log --oneline | head -1
c6e4bb8 LintLoom 1.0.0: a clickable workbench for sqlfluff

$ python -m venv .venv
$ .venv/Scripts/python -m pip install -r requirements.txt      # exit=0
$ .venv/Scripts/python -m sqlfluff --version
python -m sqlfluff, version 4.4.0

$ .venv/Scripts/python run.py --selftest                       # exit=0
catalog True python-api 28 79

$ .venv/Scripts/python -m unittest discover -s tests
Ran 100 tests in 73.179s
OK (skipped=1)
```

界面验收（服务跑在 8736）：

```
合计 24 项，通过 24，失败 0
```

含「写回真实改文件（218→249 字节）」与「备份落盘」。完整逐项记录：
`docs/evidence/11-github-clone-verify.txt`。

### 9.1 一个必须澄清的假差异

首次用 `sha256sum` 比对本地与克隆的文件，出现 5 个 DIFF。原因不是内容不同，而是
**本机全局 `core.autocrlf=true`** 让 clone 出的工作区把 LF 转成 CRLF。

正确判断方式是比较 **git blob 哈希**（不受工作区换行策略影响）：

```
$ git -C lintloom rev-parse HEAD:app/runner.py
9bdebec7037ae66eb1883221797203213a6e7d92
$ git -C _clone-lintloom rev-parse HEAD:app/runner.py
9bdebec7037ae66eb1883221797203213a6e7d92
```

`run.py / app/runner.py / app/paths.py / app/engine.py / app/web/app.js /
docs/assets/banner.svg / README.md` 全部 SAME。**结论：仓库内容与已验证内容逐字节一致。**
