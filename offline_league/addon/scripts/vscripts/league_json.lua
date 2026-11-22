local escapes = {
    ['"'] = '"', ['\\'] = '\\', ['/'] = '/',
    b = '\b', f = '\f', n = '\n', r = '\r', t = '\t',
}

local function utf8(code)
    if code < 0x80 then
        return string.char(code)
    elseif code < 0x800 then
        return string.char(0xC0 + math.floor(code / 64), 0x80 + code % 64)
    elseif code < 0x10000 then
        return string.char(0xE0 + math.floor(code / 4096), 0x80 + math.floor(code / 64) % 64, 0x80 + code % 64)
    end
    return string.char(
        0xF0 + math.floor(code / 262144),
        0x80 + math.floor(code / 4096) % 64,
        0x80 + math.floor(code / 64) % 64,
        0x80 + code % 64
    )
end

local function decode(source)
    local position = 1

    local function peek()
        return source:sub(position, position)
    end

    local function skipSpace()
        local _, finish = source:find("^[ \n\r\t]*", position)
        position = finish + 1
    end

    local function expect(token)
        if source:sub(position, position + #token - 1) ~= token then
            error("invalid JSON at byte " .. position)
        end
        position = position + #token
    end

    local function readCodePoint()
        local text = source:sub(position, position + 3)
        if not text:match("^%x%x%x%x$") then
            error("invalid JSON Unicode escape")
        end
        position = position + 4
        return tonumber(text, 16)
    end

    local function readString()
        expect('"')
        local chunks = {}
        while true do
            local special = source:find('["\\%z\1-\31]', position)
            if not special then
                error("unterminated JSON string")
            end
            chunks[#chunks + 1] = source:sub(position, special - 1)
            position = special
            if peek() == '"' then
                position = position + 1
                return table.concat(chunks)
            end
            expect('\\')
            local ch = peek()
            position = position + 1
            if ch == "u" then
                local code = readCodePoint()
                if code >= 0xD800 and code <= 0xDBFF then
                    expect("\\u")
                    local tail = readCodePoint()
                    if tail < 0xDC00 or tail > 0xDFFF then
                        error("invalid JSON surrogate pair")
                    end
                    code = 0x10000 + (code - 0xD800) * 1024 + tail - 0xDC00
                elseif code >= 0xDC00 and code <= 0xDFFF then
                    error("unexpected JSON low surrogate")
                end
                chunks[#chunks + 1] = utf8(code)
            else
                chunks[#chunks + 1] = assert(escapes[ch], "invalid JSON escape")
            end
        end
    end

    local readValue

    local function readCollection(isObject)
        local ending = isObject and '}' or ']'
        expect(isObject and '{' or '[')
        skipSpace()
        local values = {}
        if peek() == ending then
            position = position + 1
            return values
        end
        local index = 1
        while true do
            local key = index
            if isObject then
                key = readString()
                skipSpace()
                expect(':')
            end
            values[key] = readValue()
            index = index + 1
            skipSpace()
            if peek() == ending then
                position = position + 1
                return values
            end
            expect(',')
            skipSpace()
        end
    end

    function readValue()
        skipSpace()
        local ch = peek()
        if ch == '"' then
            return readString()
        elseif ch == '{' or ch == '[' then
            return readCollection(ch == '{')
        elseif ch == 't' then
            expect("true")
            return true
        elseif ch == 'f' then
            expect("false")
            return false
        elseif ch == 'n' then
            expect("null")
            return nil
        end
        local value = source:match("^-?%d+%.?%d*[eE]?[+-]?%d*", position)
        if not value or not tonumber(value) then
            error("invalid JSON value at byte " .. position)
        end
        position = position + #value
        return tonumber(value)
    end

    local result = readValue()
    skipSpace()
    if position <= #source then
        error("unexpected data after JSON")
    end
    return result
end

return { decode = decode }
