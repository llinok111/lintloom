# 来源声明 / Sources & Attribution

本项目是 **LintLoom**，一个对 `sqlfluff` 命令行能力做可视化封装的工作台。
以下内容记录上游事实、核实方式、复用边界，以及本项目的原创部分。

核实时间：**2026-10-23**（数值会变，引用时请重新核对）。

---

## 1. 上游项目

| 项 | 值 | 核实方式 |
|---|---|---|
| 仓库 | [`sqlfluff/sqlfluff`](https://github.com/sqlfluff/sqlfluff) | `GET https://api.github.com/repos/sqlfluff/sqlfluff` |
| 描述 | A modular SQL linter and auto-formatter with support for multiple dialects and templated code. | 同上 `description` |
| Star / Fork | 9,989 / 1,257 | 同上（2026-10-23 抓取） |
| 许可证 | **MIT License**（SPDX: `MIT`） | 同上 `license.spdx_id`；仓库内 `LICENSE.md` |
| 主分支 | `main` | 同上 `default_branch` |
| 最新发布 | **4.4.0**（2026-10-02） | `GET /repos/sqlfluff/sqlfluff/releases/latest` |
| 最新提交 | `1b62cf0d979ce335ffb16d8ff7cfffe6d31ab021`（2026-10-21） | `GET /repos/sqlfluff/sqlfluff/commits?per_page=1` |
| 仓库状态 | 活跃（未归档，`pushed_at` 2026-10-21） | 同上 |
| 语言 | Python | 同上 `language` |

安装到本项目虚拟环境的具体版本：`sqlfluff==4.4.0`（见 `requirements.txt`）。

---

## 2. 复用边界

### 2.1 复用了什么

1. **命令行接口**（主要方式）。本项目在子进程里调用以下公开子命令，并按官方 JSON 输出格式解析：

   | 子命令 | 用途 | 实测退出码 |
   |---|---|---|
   | `sqlfluff lint <path> -d <dialect> -f json` | 诊断 | `0` 无违规 / `1` 有违规 / `2` 执行错误 |
   | `sqlfluff fix <path> -d <dialect> -x <suffix>` | 在临时副本上修复 | `0`（即使有不可修项） |
   | `sqlfluff fix -` | 从 stdin 读、向 stdout 写修复结果 | `0` |
   | `sqlfluff parse <path> -f json` | 语法树 | `0` / `1`（含 PRS） |
   | `sqlfluff --version` | 环境自检 | `0` |

2. **公开 Python API**（仅用于读元数据，`app/catalog_probe.py`，独立子进程执行）：
   - `sqlfluff.core.Linter(config=FluffConfig(...)).rule_tuples()` → 规则码 / 名称 / 描述 / 分组 / 旧码别名
   - `sqlfluff.core.dialects.dialect_readout()` → 方言 label / 名称 / 继承关系

   这样做的原因是 `sqlfluff dialects` 的**文本输出会按 80 列截断**（实测 `materialize` 被切成
   `materializ` + 换行 `e`），拿它当权威清单不可靠；文本解析只作为兜底路径保留
   （`app/catalog.py::_parse_dialects_text`，含续行合并，且有单元测试）。

3. **未被修改的上游产物**：`pip` 安装的 `sqlfluff` 包本身按原样使用，本项目不 fork、不打补丁、
   不复制其源码到本仓库。

### 2.2 没有复用什么

- 没有复制 sqlfluff 的任何源文件、规则实现、方言定义。
- 没有使用其内部/私有 API（未触碰以 `_` 开头且非公开文档化的成员）。
- 没有把 sqlfluff 的测试夹具或示例 SQL 打包进来；`samples/` 下的样例 SQL 为本项目自写。

### 2.3 许可兼容性

- 上游 MIT，本项目 MIT，方向一致；分发时保留上游版权与许可声明（见 `NOTICE`），
  且本项目未改变上游许可条款。
- 用户如再分发本项目，需同时满足 sqlfluff 的 MIT 条款（其 `LICENSE.md` 随 pip 包安装）。

---

## 3. 本项目原创部分

| 文件 | 说明 |
|---|---|
| `app/runner.py` | 受控子进程执行器（argv-only、超时、进程树回收、输出截断、可取消句柄） |
| `app/paths.py` | 信任边界校验（根目录白名单、realpath 越界、扩展名、控制字符、方言/规则注入防护） |
| `app/jobs.py` | 作业模型（真实进度、事件日志、取消、淘汰） |
| `app/engine.py` | 编排层（修复预览 + 修复后复核、sha 并发保护、原子写回、诊断归类） |
| `app/server.py` | stdlib HTTP 服务（token、Origin、静态白名单） |
| `app/web/*` | 零依赖前端（诊断表、diff、语法树、计时条、日志、诊断卡片） |
| `tests/*` | 100 项测试，含真实 CLI 集成与安全边界用例 |
| `samples/*` | 自写样例 SQL |

---

## 4. 引用与致谢

- sqlfluff 项目与其维护者（Alan Cruickshank 等）—— 本项目的全部分析能力来自它。
- 设计参考：sqlfluff 官方文档中 `Linter` 嵌入用法与 CLI 输出格式说明。

如发现本声明与事实不符，请以 `pip show sqlfluff` 与上游仓库为准，并提 issue 修正。
