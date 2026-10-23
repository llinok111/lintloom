select o.order_id, o.amount,o.created_at from orders o where o.amount is not null order by o.created_at
