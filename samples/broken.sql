-- 故意留下的语法错误：用来演示「解析失败（PRS）」诊断，这类问题不会被自动修复
select
    o.id,
    o.amount,
from orders o
where o.status =
