local progression = {}
local tierLevels = { 10, 15, 20, 25 }
local heroPlans = setmetatable({}, { __mode = "k" })

local function resolveTalents(bot)
    local talents = {}
    for slot = 0, 25 do
        local ability = bot:GetAbilityInSlot(slot)
        if ability and ability:IsTalent() and ability:GetName() ~= "special_bonus_attributes" then
            talents[#talents + 1] = ability
        end
    end
    if #talents == 8 then return talents end
end

local function separatePlan(state, skills)
    local tiers = {}
    for index, ability in ipairs(state.talents) do
        tiers[ability:GetName()] = math.ceil(index / 2)
    end

    local preferred = {}
    for _, name in ipairs(skills) do
        local tier = tiers[name]
        if tier and not preferred[tier] then preferred[tier] = name end
    end
    for tier, name in pairs(preferred) do state.preferred[tier] = name end
    for index = #skills, 1, -1 do
        if tiers[skills[index]] then table.remove(skills, index) end
    end
    state.skills = skills
end

function progression.learn(bot, skills)
    local state = heroPlans[bot]
    if not state then
        local talents = resolveTalents(bot)
        if not talents then return end
        state = { talents = talents, preferred = {} }
        heroPlans[bot] = state
    end
    if state.skills ~= skills then separatePlan(state, skills) end

    local level = bot:GetLevel()
    local learned = 0
    for tier, required in ipairs(tierLevels) do
        if level < required then return end

        local first, second = state.talents[tier * 2 - 1], state.talents[tier * 2]
        local firstLearned, secondLearned = first:GetLevel() > 0, second:GetLevel() > 0
        if not firstLearned and not secondLearned then
            local name = state.preferred[tier] or first:GetName()
            bot:ActionImmediate_LevelAbility(name)
            return
        end
        learned = learned + (firstLearned and 1 or 0) + (secondLearned and 1 or 0)
    end

    -- Extra talent points unlock the other option at levels 27 to 30.
    if level >= 27 and learned < math.min(8, level - 22) then
        for _, talent in ipairs(state.talents) do
            if talent:GetLevel() == 0 then
                bot:ActionImmediate_LevelAbility(talent:GetName())
                return
            end
        end
    end
end

return progression
