-- Sliding window: at most 3 admitted requests in any 5 seconds per user.
-- Redis time avoids clock skew between clients. Unique members avoid collisions.
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 5000)
if redis.call('ZCARD', KEYS[1]) >= 3 then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return math.max(1, tonumber(oldest[2]) + 5000 - now)
end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 5000)
return 0
