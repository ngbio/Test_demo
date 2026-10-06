-- KEYS: token hash, user guard, stock. ARGV: user, event, product, nonce.
-- No other request can interleave between these checks and the writes.
if redis.call('EXISTS', KEYS[1]) == 0 then return {'TOKEN_NOT_FOUND'} end
if redis.call('HGET', KEYS[1], 'user') ~= ARGV[1]
    or redis.call('HGET', KEYS[1], 'event') ~= ARGV[2]
    or redis.call('HGET', KEYS[1], 'product') ~= ARGV[3] then
    return {'TOKEN_MISMATCH'}
end
if redis.call('HGET', KEYS[1], 'state') == 'USED' then
    return {'TOKEN_ALREADY_USED'}
end
local now = tonumber(redis.call('TIME')[1])
if now >= tonumber(redis.call('HGET', KEYS[1], 'expires')) then
    return {'TOKEN_EXPIRED'}
end
local guard = redis.call('GET', KEYS[2])
if guard == 'PURCHASED' then return {'ALREADY_PURCHASED'} end
if guard then return {'ALREADY_PROCESSING'} end
local stock = redis.call('GET', KEYS[3])
if not stock then return {'DEMO_NOT_INITIALIZED'} end
if tonumber(stock) <= 0 then return {'SOLD_OUT'} end

local remaining = redis.call('DECR', KEYS[3])
-- No reservation TTL: expiring a reservation while DB is committing is unsafe.
redis.call('SET', KEYS[2], ARGV[4])
redis.call('HSET', KEYS[1], 'state', 'USED')
return {'RESERVED', tostring(remaining)}
