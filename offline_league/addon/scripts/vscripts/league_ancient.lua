modifier_offline_league_ancient = class({})

function modifier_offline_league_ancient:IsHidden() return true end
function modifier_offline_league_ancient:IsPurgable() return false end

function modifier_offline_league_ancient:DeclareFunctions()
    return { MODIFIER_PROPERTY_MIN_HEALTH, MODIFIER_EVENT_ON_DEATH_PREVENTED }
end

function modifier_offline_league_ancient:GetMinHealth()
    return 1
end

function modifier_offline_league_ancient:OnDeathPrevented(event)
    if IsServer() and event.unit == self:GetParent() and GameRules.OfflineLeague then
        GameRules.OfflineLeague:confirmAncientDeath(event.unit, event.attacker)
    end
end
