-- Retain the record for an hour to distinguish expired / used tokens.
-- The actual purchase validity is only 60 seconds, checked in reserve.lua.
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local now = tonumber(redis.call('TIME')[1])
redis.call('HSET', KEYS[1],
    'user', ARGV[1], 'event', ARGV[2], 'product', ARGV[3],
    'expires', now + 60, 'state', 'NEW')
redis.call('EXPIRE', KEYS[1], 3600)
return 1
