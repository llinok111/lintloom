select a.id,a.name , sum(b.amount) as total
from orders a join order_items b on a.id=b.order_id
where a.created_at > '2024-01-01' and b.status<>'cancelled'
group by a.id,a.name
having sum(b.amount)>100
order by 3 desc
