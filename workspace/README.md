# workspace —— 可写工作区

界面里的「根目录」下拉框有两个来源：

- `samples`：只读演示数据，请勿在此演练「应用修复」。
- `workspace`：你自己的 SQL 放这里，可以随意写回（备份会自动落在 `.lintloom-backups/`）。

想加更多目录，用启动参数指定（浏览器端无权自行添加，这是有意的安全设计）：

```bat
".venv\Scripts\python.exe" run.py --root dwh=D:\data\warehouse
```

`--root` 的标识只能是「小写字母开头 + 小写字母/数字/下划线/连字符」，路径必须已存在。
