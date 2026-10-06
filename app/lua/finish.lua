-- KEYS: user guard, stock. ARGV: owning nonce, COMMIT/ROLLBACK/DUPLICATE.
-- Ownership check makes compensation idempotent: never restore stock twice.
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == 'COMMIT' then
    redis.call('SET', KEYS[1], 'PURCHASED')
else
    redis.call('INCR', KEYS[2])
    if ARGV[2] == 'DUPLICATE' then
        redis.call('SET', KEYS[1], 'PURCHASED')
    else
        redis.call('DEL', KEYS[1])
    end
end
-- Token remains USED even after rollback; retry requires a fresh token.
return 1
