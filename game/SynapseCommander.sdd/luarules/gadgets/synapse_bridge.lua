function gadget:GetInfo()
  return {
    name = "Synapse mailbox bridge",
    desc = "Bounded JSONL intent bridge for the Synapse smoke loop",
    author = "Synapse Commander",
    layer = 0,
    enabled = true,
  }
end

local Json = Json or VFS.Include("common/luaUtilities/json.lua")
local PREFIX, MAX_LINE = "SYN0 ", 4096

if gadgetHandler:IsSyncedCode() then
  -- Keep enough ids to distinguish a stale observation from an unknown one.
  -- The public observation itself remains bounded; this is reducer-only state.
  local TEAM0, OBS_INTERVAL, MAX_OBSERVATIONS, MAX_REPORTS = 0, 15, 128, 32
  local observations, observation_order, seen, pending, rate = {}, {}, {}, {}, {}
  local waypoint, scenario_emitted = nil, false
  -- Factory provisioning (rah26-8zp), active only when the synapse_scenario
  -- mod option names a ScenarioTable/v1 file in this archive.  Every count
  -- and unit name comes from that versioned table, never from this code.
  local SCENARIO_LIMIT, MAX_JOBS = 16384, 8
  local scenario, budgets, resources, commander, factory = nil, nil, nil, nil, nil
  local scenario_origin_x, scenario_origin_z = nil, nil
  local extracted, lost, initial_losses, extraction_dwell, mission_outcome = {}, {}, {}, {}, nil
  local jobs, job_seq, job_spawning, embodied, commander_lost = {}, 0, nil, {}, false
  -- Initial world (rah26-5hf). Assets hold evaluator truth; principal-mode
  -- observations derive only from each body and delivered reports.
  local assets, asset_order, hidden, reports = {}, {}, {}, {}
  local carrying_parts, entrapment_state, exposure_state, parts_consumed = {}, nil, nil, 0
  local spawning_hidden = false
  -- Per-principal state is enabled only by a declared principals table.
  -- In that mode observations are calculated from geometry below, never from
  -- team LOS or an engine sensor-radius mutation.
  local principal_mode, principals, principal_order, unit_principal, principal_body_ids = false, {}, {}, {}, {}
  local links, delivered_reports, queued_reports, entity_names = {}, {}, {}, {}
  local factory_name = "F0"
  local terrain_method = Spring.TraceRayGroundBetweenPositions and "native" or "sampled"
  local effect, body_for_principal
  local waypoints_world = {}
  local scenario_failed = false

  local function identifier(value)
    return type(value) == "string" and #value > 0 and #value <= 64 and value:match("^[%w_.-]+$") ~= nil
  end

  -- Evaluator-only cause tokens may compose bounded vocabulary terms, e.g.
  -- terrain_entrapment+parts. They never enter a principal observation.
  local function cause_identifier(value)
    return type(value) == "string" and #value > 0 and #value <= 64 and value:match("^[%w_.+%-]+$") ~= nil
  end

  local function count(value)
    return type(value) == "number" and value >= 0 and value % 1 == 0
  end

  local function offset_pair(value)
    return type(value) == "table" and type(value[1]) == "number" and type(value[2]) == "number"
  end

  local function load_scenario(name)
    if not identifier(name) then return nil, "invalid_name" end
    local raw = VFS.LoadFile("scenarios/" .. name .. ".json")
    if type(raw) ~= "string" or #raw == 0 then return nil, "unavailable" end
    if #raw > SCENARIO_LIMIT then return nil, "too_large" end
    local ok, t = pcall(Json.decode, raw)
    if not ok or type(t) ~= "table" then return nil, "not_json" end
    if t.schema ~= "ScenarioTable/v1" or t.id ~= name or not identifier(t.version) then return nil, "unsupported" end
    local f = t.factory
    if type(f) ~= "table" or not identifier(f.name) or not identifier(f.def) or not UnitDefNames[f.def] or not offset_pair(f.offset) then
      return nil, "invalid_factory"
    end
    if not count(t.bodies) or type(t.chips) ~= "table" then return nil, "invalid_inventory" end
    local chips = 0
    for id, n in pairs(t.chips) do
      if not identifier(id) or not count(n) then return nil, "invalid_inventory" end
      chips = chips + 1
    end
    if chips < 1 or chips > 8 then return nil, "invalid_inventory" end
    if type(t.commander) ~= "table" or not identifier(t.commander.chip) or t.chips[t.commander.chip] == nil
      or (t.commander.position ~= nil and not offset_pair(t.commander.position))
      or (t.commander.capabilities ~= nil and type(t.commander.capabilities) ~= "table") then
      return nil, "invalid_commander"
    end
    for _, verb in ipairs(t.commander.capabilities or {}) do if verb ~= "move" and verb ~= "sense" and verb ~= "assist" then return nil, "invalid_commander" end end
    if type(t.chassis) ~= "table" then return nil, "invalid_chassis" end
    local kinds = 0
    for id, c in pairs(t.chassis) do
      if not identifier(id) or type(c) ~= "table" or not identifier(c.def) or not UnitDefNames[c.def]
        or not count(c.build_frames) or not count(c.commission_frames)
        or (c.capabilities ~= nil and type(c.capabilities) ~= "table") then
        return nil, "invalid_chassis"
      end
      if c.speed_factor ~= nil and (type(c.speed_factor) ~= "number" or c.speed_factor < 0.05 or c.speed_factor > 1) then return nil, "invalid_chassis" end
      if c.carries_parts ~= nil and type(c.carries_parts) ~= "boolean" then return nil, "invalid_chassis" end
      for _, verb in ipairs(c.capabilities or {}) do if verb ~= "move" and verb ~= "sense" and verb ~= "assist" and verb ~= "relay" then return nil, "invalid_chassis" end end
      kinds = kinds + 1
    end
    if kinds < 1 or kinds > 8 then return nil, "invalid_chassis" end
    if t.commander.speed_factor ~= nil and (type(t.commander.speed_factor) ~= "number" or t.commander.speed_factor < 0.05 or t.commander.speed_factor > 1) then return nil, "invalid_commander" end
    if t.resources ~= nil and (type(t.resources) ~= "table" or not count(t.resources.parts or 0)) then return nil, "invalid_resources" end
    local w1 = type(t.waypoints) == "table" and t.waypoints.W1
    if type(w1) ~= "table" or type(w1[1]) ~= "number" or type(w1[3]) ~= "number" then return nil, "invalid_waypoint" end
    -- Optional initial-world fields.  The table is the fully chosen world for
    -- its (id, version, seed): the gadget draws no randomness of its own.
    if t.seed ~= nil and not count(t.seed) then return nil, "invalid_seed" end
    local names, active_names, bodies_needed, chips_needed = {}, {}, 1, { [t.commander.chip] = 1 }
    if t.predeployed ~= nil then
      if type(t.predeployed) ~= "table" or #t.predeployed > 8 then return nil, "invalid_predeployed" end
      for _, a in ipairs(t.predeployed) do
        if type(a) == "table" and a.controller == Json.null then a.controller = nil end
        if type(a) ~= "table" or not identifier(a.name) or names[a.name] or not identifier(a.def) or not UnitDefNames[a.def]
          or (a.controller ~= nil and (not identifier(a.controller) or t.chips[a.controller] == nil))
          or not offset_pair(a.position) or (a.state ~= "active" and a.state ~= "destroyed") or type(a.contact) ~= "boolean"
          or (a.health ~= nil and (type(a.health) ~= "number" or a.health <= 0 or a.health > 1))
          or (a.speed_factor ~= nil and (type(a.speed_factor) ~= "number" or a.speed_factor < 0.05 or a.speed_factor > 1)) then
          return nil, "invalid_predeployed"
        end
        names[a.name] = true
        if a.state == "active" then active_names[a.name] = true end
        bodies_needed = bodies_needed + 1
        if a.controller then chips_needed[a.controller] = (chips_needed[a.controller] or 0) + 1 end
      end
    end
    -- Every deployed body and chip comes out of the declared inventory.
    if bodies_needed > t.bodies then return nil, "inventory_insufficient" end
    for chip, n in pairs(chips_needed) do
      if n > t.chips[chip] then return nil, "inventory_insufficient" end
    end
    local d = t.distress
    if d ~= nil then
      if type(d) ~= "table" or not names[d.asset] or type(d.last_known) ~= "table" or not offset_pair(d.last_known.position)
        or not count(d.last_known.age_frames) or not identifier(d.symptom) or not cause_identifier(d.cause_hidden) then
        return nil, "invalid_distress"
      end
    end
    local entrapment = t.entrapment
    if entrapment ~= nil and entrapment["until"] ~= nil then
      if type(entrapment) == "table" and entrapment["until"] == "towed" then return nil, "table_invalid" end
      if type(entrapment) ~= "table" or not names[entrapment.asset] or entrapment["until"] ~= "parts_delivered"
        or type(entrapment.assist_capability) ~= "string" or not count(entrapment.assist_range) or entrapment.assist_range <= 0
        or not count(entrapment.assist_frames) or entrapment.assist_frames <= 0
        or type(entrapment.parts_carrier_chassis) ~= "table" or #entrapment.parts_carrier_chassis < 1 then return nil, "invalid_entrapment" end
      local listed = {}
      for _, chassis in ipairs(entrapment.parts_carrier_chassis) do
        if type(chassis) ~= "string" or listed[chassis] or not t.chassis[chassis] or not t.chassis[chassis].carries_parts then return nil, "invalid_entrapment" end
        listed[chassis] = true
      end
      if entrapment.beacon_only ~= nil and type(entrapment.beacon_only) ~= "boolean" then return nil, "invalid_entrapment" end
      if entrapment.beacon_fix ~= nil then
        local fix = entrapment.beacon_fix
        if type(fix) ~= "table" or (fix.kind ~= "bearing" and fix.kind ~= "quadrant") then return nil, "invalid_entrapment" end
        if fix.kind == "bearing" then
          if fix.from ~= "commander" or type(fix.error_deg) ~= "number" or fix.error_deg < 0 or fix.error_deg > 90 then return nil, "invalid_entrapment" end
        elseif not count(fix.cell_elmos) or fix.cell_elmos < 1 or fix.cell_elmos > 8192 then
          return nil, "invalid_entrapment"
        end
      elseif entrapment.beacon_only then
        return nil, "invalid_entrapment"
      end
    end
    local exposure = t.exposure
    if exposure ~= nil then
      if type(exposure) ~= "table" or not active_names[exposure.asset]
        or type(exposure.decay_per_minute) ~= "number" or exposure.decay_per_minute <= 0 or exposure.decay_per_minute > 1
        or type(exposure.floor) ~= "number" or exposure.floor <= 0 or exposure.floor > 1 then return nil, "invalid_exposure" end
    end
    -- Principal mode is deliberately opt-in for backwards-compatible Loop 1
    -- tables.  Every listed non-commander principal names a deployed asset;
    -- the commander principal is the engine-owned commander body.
    if t.principals ~= nil then
      if type(t.principals) ~= "table" or t.principals.commander == nil then return nil, "invalid_principals" end
      local principal_count, predeployed_by_name = 0, {}
      for _, a in ipairs(t.predeployed or {}) do predeployed_by_name[a.name] = a end
      for name, p in pairs(t.principals) do
        if not identifier(name) or type(p) ~= "table" or not identifier(p.chip) or t.chips[p.chip] == nil then return nil, "invalid_principals" end
        if name ~= "commander" and (not predeployed_by_name[name] or predeployed_by_name[name].controller ~= p.chip) then return nil, "invalid_principals" end
        principal_count = principal_count + 1
      end
      if principal_count < 1 or principal_count > 8 or t.principals.commander.chip ~= t.commander.chip then return nil, "invalid_principals" end
      if type(t.reasoner_chips) ~= "table" or #t.reasoner_chips < 1 or #t.reasoner_chips > 8 then return nil, "invalid_reasoner_chips" end
      local reasoner = {}
      for _, chip in ipairs(t.reasoner_chips) do
        if not identifier(chip) or t.chips[chip] == nil or reasoner[chip] then return nil, "invalid_reasoner_chips" end
        reasoner[chip] = true
      end
      for _, p in pairs(t.principals) do if not reasoner[p.chip] then return nil, "invalid_reasoner_chips" end end
      if type(t.sensors) ~= "table" then return nil, "invalid_sensors" end
      for key, radius in pairs(t.sensors) do if not identifier(key) or type(radius) ~= "number" or radius < 0 or radius > 8192 then return nil, "invalid_sensors" end end
      local c = t.comms
      if type(c) ~= "table" or type(c.range) ~= "number" or c.range <= 0 or c.range > 8192
        or not count(c.latency_frames) or c.latency_frames > 900 or type(c.relay_chassis) ~= "table" then return nil, "invalid_comms" end
      for _, chassis in ipairs(c.relay_chassis) do if type(chassis) ~= "string" or not t.chassis[chassis] then return nil, "invalid_comms" end end
    end
    return t
  end

  local function sorted_keys(map)
    local keys = {}
    for key in pairs(map) do keys[#keys + 1] = key end
    table.sort(keys)
    return keys
  end

  local function squared_distance(x1, z1, x2, z2)
    return (x1 - x2) * (x1 - x2) + (z1 - z2) * (z1 - z2)
  end

  local function beacon_isolated(unitID)
    return entrapment_state and entrapment_state.beacon_only and not entrapment_state.delivered
      and entrapment_state.unit_id == unitID
  end

  local function beacon_principal(principal)
    return entrapment_state and entrapment_state.beacon_only and not entrapment_state.delivered
      and entrapment_state.target == principal
  end

  local function beacon_bearing(from_x, from_z, target_x, target_z, error_deg, seed)
    local dx, dz = target_x - from_x, target_z - from_z
    local angle = math.deg(math.atan(dz / (dx == 0 and 0.000001 or dx)))
    if dx < 0 then angle = angle + 180 end
    if angle < 0 then angle = angle + 360 end
    -- The table seed is the complete source of the bounded perturbation.
    local offset = (((seed or 0) % 2001) / 1000 - 1) * error_deg
    return (angle + offset + 360) % 360
  end

  -- The pinned engine exports TraceRayGroundBetweenPositions to synced Lua.
  -- Keep the sampling fallback because a missing/changed export must fail
  -- closed as a terrain occluder rather than silently falling back to team LOS.
  local function terrain_clear(x1, y1, z1, x2, y2, z2)
    local ray = Spring.TraceRayGroundBetweenPositions
    if ray then
      local ok, hit = pcall(ray, x1, y1 + 16, z1, x2, y2 + 16, z2)
      -- Recoil returns false/nil for a clear segment and hitDistance for terrain.
      if ok then return hit == nil or hit == false end
      terrain_method = "sampled"
    end
    local distance = math.sqrt(squared_distance(x1, z1, x2, z2))
    local steps = math.max(1, math.ceil(distance / 16))
    for i = 1, steps - 1 do
      local t = i / steps
      local x, z = x1 + (x2 - x1) * t, z1 + (z2 - z1) * t
      local eye = y1 + 16 + (y2 - y1) * t
      if Spring.GetGroundHeight(x, z) > eye then return false end
    end
    return true
  end

  local function unit_sensor_range(unitID)
    local definition = UnitDefs[Spring.GetUnitDefID(unitID)] or {}
    local radius = scenario and scenario.sensors and scenario.sensors[definition.name] or nil
    radius = radius or definition.losRadius or 0
    local health, max_health = Spring.GetUnitHealth(unitID)
    if health and max_health and max_health > 0 then
      local fraction = health / max_health
      if fraction < 0.5 then radius = radius * math.max(0, fraction / 0.5) end
    end
    return radius
  end

  local function detects(observer, target)
    if observer == target then return true end
    local ox, oy, oz = Spring.GetUnitPosition(observer)
    local tx, ty, tz = Spring.GetUnitPosition(target)
    if not ox or not tx or squared_distance(ox, oz, tx, tz) > unit_sensor_range(observer) ^ 2 then return false end
    return terrain_clear(ox, oy, oz, tx, ty, tz)
  end

  local function chassis_for_unit(unitID)
    -- A destroyed or never-spawned asset has no numeric unit id (live-11: H0 is
    -- spawned destroyed), and a stale id may already be invalid; neither may
    -- reach the engine lookup.
    if not scenario or type(unitID) ~= "number" or not Spring.ValidUnitID(unitID) then return nil end
    local defID = Spring.GetUnitDefID(unitID)
    local definition = defID and UnitDefs[defID]
    for id, chassis in pairs(scenario.chassis) do
      if definition and chassis.def == definition.name then return id end
    end
    return nil
  end

  local asset_for_unit

  local function speed_factor_for_unit(unitID)
    if commander and commander.unit_id == unitID then return scenario.commander.speed_factor or 1 end
    local _, asset = asset_for_unit(unitID)
    if asset and asset.speed_factor then return asset.speed_factor end
    local chassis = chassis_for_unit(unitID)
    return chassis and (scenario.chassis[chassis].speed_factor or 1) or 1
  end

  local function apply_speed(unitID, health_factor, held)
    local definition = UnitDefs[Spring.GetUnitDefID(unitID)]
    if Spring.MoveCtrl and Spring.MoveCtrl.SetGroundMoveTypeData and definition and definition.speed then
      local speed = held and 0 or math.min(definition.speed, definition.speed * speed_factor_for_unit(unitID) * (health_factor or 1))
      pcall(Spring.MoveCtrl.SetGroundMoveTypeData, unitID, "maxSpeed",
            speed)
      return speed
    end
    return nil
  end

  local function chassis_carries_parts(chassis)
    return chassis and scenario.chassis[chassis] and scenario.chassis[chassis].carries_parts == true
  end

  local function allocate_parts(chassis)
    if chassis_carries_parts(chassis) and resources and resources.parts > 0 then
      resources.parts = resources.parts - 1
      return true
    end
    return false
  end

  asset_for_unit = function(unitID)
    for _, name in ipairs(asset_order) do
      if assets[name].unit_id == unitID then return name, assets[name] end
    end
    return nil, nil
  end

  local function health_factor_for_asset(asset)
    if exposure_state and exposure_state.unit_id == asset.unit_id then return exposure_state.cap end
    return asset.health or 1
  end

  local function held_for_unit(unitID)
    return (entrapment_state and not entrapment_state.delivered and entrapment_state.unit_id == unitID)
      or (exposure_state and exposure_state.immobilised and exposure_state.unit_id == unitID)
  end

  local function principal_for_unit(unitID)
    if unit_principal[unitID] then return unit_principal[unitID] end
    if principal_mode and Spring.GetUnitTeam(unitID) == TEAM0 then return "commander" end
    return nil
  end

  local function is_relay_node(unitID)
    if commander and commander.unit_id == unitID then return true end
    if factory and factory.unit_id == unitID then return true end
    if not scenario then return false end
    local chassis = chassis_for_unit(unitID)
    for _, id in ipairs(scenario.comms.relay_chassis) do if id == chassis then return true end end
    return false
  end

  local function is_hq_node(unitID)
    return commander and commander.unit_id == unitID
  end

  local function recompute_links(frame)
    if not principal_mode then return end
    local units, reachable, queue = {}, {}, {}
    for _, unitID in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do
      -- The engine boundary is numeric.  Normalize before using an id in an
      -- API call or ordering it: an unexpected proxy value must be skipped,
      -- not reach GetUnitDefID through the link reducer.
      unitID = tonumber(unitID)
      if unitID and Spring.ValidUnitID(unitID) then units[#units + 1] = unitID end
    end
    table.sort(units)
    for _, unitID in ipairs(units) do
      if is_hq_node(unitID) then reachable[unitID] = true; queue[#queue + 1] = unitID end
    end
    local first = 1
    while first <= #queue do
      local from = queue[first]; first = first + 1
      local fx, fy, fz = Spring.GetUnitPosition(from)
      if fx then
        for _, target in ipairs(units) do
          if not reachable[target] then
            local tx, ty, tz = Spring.GetUnitPosition(target)
            if tx and squared_distance(fx, fz, tx, tz) <= scenario.comms.range ^ 2 and terrain_clear(fx, fy, fz, tx, ty, tz) then
              reachable[target] = true
              if is_relay_node(target) then queue[#queue + 1] = target end
            end
          end
        end
      end
    end
    for _, unitID in ipairs(units) do
      local prior, current = links[unitID], reachable[unitID] == true
      if beacon_isolated(unitID) then current = false end
      links[unitID] = current
      if prior ~= nil and prior ~= current then
        local principal = principal_for_unit(unitID)
        if principal then
          effect(current and "link_restored" or "link_lost", frame, unitID, nil, nil, principal)
        end
      end
    end
  end

  local function entity_for(unitID, source)
    local x, y, z = Spring.GetUnitPosition(unitID)
    if not x then return nil end
    local definition = UnitDefs[Spring.GetUnitDefID(unitID)]
    local body = { id = unitID, team = Spring.GetUnitTeam(unitID), pos = { x, y, z }, source = source,
                   def = definition and definition.name }
    if source == "own_body" or source == "own_unit" then
      body.health, body.max_health = Spring.GetUnitHealth(unitID)
      body.commands = Spring.GetUnitCommandCount(unitID)
      body.controller = embodied[unitID]
      body.speed_factor = speed_factor_for_unit(unitID)
      if source == "own_body" then
        body.sensor_range = unit_sensor_range(unitID)
        body.link = links[unitID] == true
      end
      if carrying_parts[unitID] then body.carrying_parts = true end
      if entrapment_state and not entrapment_state.delivered and entrapment_state.unit_id == unitID then
        body.immobilised, body.reason = true, "needs_parts"
      elseif exposure_state and exposure_state.immobilised and exposure_state.unit_id == unitID then
        body.immobilised, body.reason = true, "exposure"
      end
      if exposure_state and exposure_state.unit_id == unitID then
        body.exposure = { decay_per_minute = exposure_state.config.decay_per_minute, health_now = exposure_state.cap }
      end
    end
    return body
  end

  local function relative_entity_for(observerID, unitID, source)
    local entity = entity_for(unitID, source)
    if not entity then return nil end
    local ox, _, oz = Spring.GetUnitPosition(observerID)
    local tx, _, tz = Spring.GetUnitPosition(unitID)
    if not ox or not tx then return nil end
    local dx, dz = tx - ox, tz - oz
    local angle = math.deg(math.atan(dz / (dx == 0 and 0.000001 or dx)))
    if dx < 0 then angle = angle + 180 end
    if angle < 0 then angle = angle + 360 end
    entity.pos = nil
    entity.distance = math.floor(math.sqrt(dx * dx + dz * dz) + 0.5)
    entity.bearing_deg = math.floor(angle + 0.5) % 360
    return entity
  end

  local function out(kind, body)
    SendToUnsynced("synapse_out", kind, Json.encode(body))
  end

  local function valid_tree(value, depth)
    if type(value) ~= "table" then return true end
    if depth > 4 then return false end
    for key, item in pairs(value) do
      if type(key) ~= "string" and type(key) ~= "number" then return false end
      if not valid_tree(item, depth + 1) then return false end
    end
    return true
  end

  local forbidden = { lua = true, command = true, cmd_id = true, script = true }
  local function has_raw_engine(value, depth)
    if type(value) ~= "table" then return false end
    if depth > 4 then return true end
    for key, item in pairs(value) do
      if type(key) == "string" and forbidden[key:lower()] then return true end
      if has_raw_engine(item, depth + 1) then return true end
    end
    return false
  end

  local function emit_receipt(intent, status, reason, detail)
    local id = type(intent) == "table" and intent.intent_id or nil
    local body = {
      schema = "IntentReceipt/v1",
      intent_id = type(id) == "string" and id or "invalid",
      principal = type(intent) == "table" and intent.principal or nil,
      team = type(intent) == "table" and tonumber(intent.team) or nil,
      status = status,
      reason = reason,
      frame = Spring.GetGameFrame(),
    }
    if detail then body.detail = detail end
    out("receipt", body)
  end

  -- K1 keeps the pre-principal public boundary intact: a single team view
  -- uses engine LOS, while principal-mode observations below use only body
  -- geometry.  Do not route this compatibility path through detects(), whose
  -- observer must never become the candidate unit itself.
  local function legacy_visible_source(unitID, teamID, allyTeam)
    if hidden[unitID] then return nil end
    if Spring.GetUnitTeam(unitID) == TEAM0 then return "own_unit" end
    local state = Spring.GetUnitLosState(unitID, allyTeam, false)
    if state and state.los then return "los" end
    return nil
  end

  local function can_emit_effect(unitID, teamID)
    -- Legacy Loop 0 has one team view; effects obey that same projection.
    if principal_mode then return false end
    local _, _, _, _, _, allyTeam = Spring.GetTeamInfo(TEAM0, false)
    return legacy_visible_source(unitID, teamID, allyTeam) ~= nil
  end

  local function observation_known(scope, id)
    local by_team = observations[scope]
    return by_team and by_team[id]
  end

  local function use_token(teamID, frame)
    local bucket = rate[teamID] or { tokens = 4, frame = frame }
    local elapsed = frame - bucket.frame
    if elapsed >= 30 then
      bucket.tokens = math.min(4, bucket.tokens + math.floor(elapsed / 30) * 2)
      bucket.frame = bucket.frame + math.floor(elapsed / 30) * 30
    end
    if bucket.tokens < 1 then rate[teamID] = bucket; return false end
    bucket.tokens = bucket.tokens - 1
    rate[teamID] = bucket
    return true
  end

  local function remember_observation(scope, id, frame)
    local by_team = observations[scope] or {}
    local order = observation_order[scope] or {}
    observations[scope] = by_team
    observation_order[scope] = order
    if not by_team[id] then order[#order + 1] = id end
    by_team[id] = frame
    while #order > MAX_OBSERVATIONS do
      by_team[table.remove(order, 1)] = nil
    end
  end

  effect = function(name, frame, unitID, intent_id, detail, principal)
    if principal_mode and not principal then return end
    local body = { schema = "EffectEvent/v1", effect = name, frame = frame, unit_id = unitID, team = TEAM0, intent_id = intent_id, detail = detail }
    if principal_mode then body.principal = principal end
    out("effect", body)
  end

  local function effect_to_target_viewers(name, frame, target_name, targetID, detail)
    local sent, owner = {}, principal_for_unit(targetID)
    if owner then
      effect(name, frame, targetID, nil, detail, owner)
      sent[owner] = true
    end
    for _, principal in ipairs(principal_order) do
      local bodyID = body_for_principal(principal)
      if bodyID and not sent[principal] and detects(bodyID, targetID) then
        effect(name, frame, targetID, nil, detail, principal)
        sent[principal] = true
      end
    end
  end

  local function stop_unit(unitID)
    if Spring.ValidUnitID(unitID) then Spring.GiveOrderToUnit(unitID, CMD.STOP, {}, 0) end
  end

  -- The embodied commander is BAR's own team-0 commander.  Binding it spends
  -- one body and the declared chip, because the body cap counts every mobile
  -- embodiment; an unbudgeted commander stays unbound and the team cannot act.
  local function bind_commander(unitID)
    if not scenario or commander or commander_lost or Spring.GetUnitTeam(unitID) ~= TEAM0 then return end
    local definition = UnitDefs[Spring.GetUnitDefID(unitID)]
    if not (definition and definition.customParams and definition.customParams.iscommander) then return end
    local chip = scenario.commander.chip
    if budgets.bodies_remaining < 1 or (budgets.chips[chip] or 0) < 1 then
      out("bridge_error", { reason = "commander_unbudgeted", unit_id = unitID })
      return
    end
    budgets.bodies_remaining = budgets.bodies_remaining - 1
    budgets.chips[chip] = budgets.chips[chip] - 1
    commander = { unit_id = unitID, chip = chip }
    if factory and factory.unit_id then
      local fx, _, fz = Spring.GetUnitPosition(factory.unit_id)
      local offset = scenario.commander.position or { 0, 240 }
      if fx then
        Spring.SetUnitPosition(unitID, fx + offset[1], fz + offset[2])
        local cx, cy, cz = Spring.GetUnitPosition(unitID)
        commander.position = { cx, cy, cz }
        effect("commander_placed", Spring.GetGameFrame(), unitID, nil, { position = commander.position }, principal_mode and "commander" or nil)
      end
    end
    embodied[unitID] = chip
    apply_speed(unitID, 1, false)
    if principal_mode then unit_principal[unitID], principal_body_ids[unitID] = "commander", true end
    effect("commander_bound", Spring.GetGameFrame(), unitID, nil, { chip = chip }, principal_mode and "commander" or nil)
  end

  -- Observation age and expiry, shared by TASK and PRODUCE in this order.
  local function freshness_problem(intent, scope, frame)
    local observation_frame = observation_known(scope, intent.based_on_observation_id)
    if not observation_frame then return "unknown_observation" end
    if frame - observation_frame > 900 then return "stale_observation" end
    if type(intent.expires_at_frame) ~= "number" or intent.expires_at_frame < frame then return "expired" end
    if intent.expires_at_frame > frame + 900 then return "expiry_out_of_range" end
    return nil
  end

  local function job_detail(job)
    return { job_id = job.job_id, factory = factory.name, chassis = job.chassis, controller = job.controller,
             parts = job.carrying_parts and 1 or 0 }
  end

  local PRODUCE_FIELDS = { factory = true, chassis = true, controller = true, parameters = true }

  -- Acceptance spends one body and one chip immediately; jobs cannot be
  -- cancelled.  The factory builds one job at a time, so a queued job starts
  -- when the previous one is due.
  local function reduce_produce(intent, id, teamID)
    local payload, frame = intent.payload, Spring.GetGameFrame()
    if principal_mode and intent.principal ~= "commander" then emit_receipt(intent, "rejected", "not_authorized"); return true end
    for key in pairs(payload) do
      if not PRODUCE_FIELDS[key] then emit_receipt(intent, "rejected", "invalid_payload"); return true end
    end
    if payload.factory ~= factory.name or not factory.unit_id or not Spring.ValidUnitID(factory.unit_id)
      or Spring.GetUnitTeam(factory.unit_id) ~= teamID then
      emit_receipt(intent, "rejected", "unknown_factory"); return true
    end
    local chassis = type(payload.chassis) == "string" and scenario.chassis[payload.chassis]
    if not chassis then emit_receipt(intent, "rejected", "unknown_chassis"); return true end
    if type(payload.controller) ~= "string" or budgets.chips[payload.controller] == nil then
      emit_receipt(intent, "rejected", "unknown_controller"); return true
    end
    if type(payload.parameters) ~= "table" or next(payload.parameters) ~= nil then
      emit_receipt(intent, "rejected", "invalid_parameters"); return true
    end
    local problem = freshness_problem(intent, principal_mode and intent.principal or teamID, frame)
    if problem then emit_receipt(intent, "rejected", problem); return true end
    if #jobs >= MAX_JOBS then emit_receipt(intent, "rejected", "factory_queue_full"); return true end
    if budgets.bodies_remaining < 1 then emit_receipt(intent, "rejected", "budget_exhausted"); return true end
    if budgets.chips[payload.controller] < 1 then emit_receipt(intent, "rejected", "chip_unavailable"); return true end
    if not use_token(principal_mode and intent.principal or teamID, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
    budgets.bodies_remaining = budgets.bodies_remaining - 1
    budgets.chips[payload.controller] = budgets.chips[payload.controller] - 1
    job_seq = job_seq + 1
    local previous = jobs[#jobs]
    local start = previous and math.max(previous.done_frame, frame) or frame
    local job = {
      job_id = "job-" .. job_seq, seq = job_seq, intent_id = id, chassis = payload.chassis, controller = payload.controller,
      started_frame = start, done_frame = start + chassis.build_frames + chassis.commission_frames, started = false,
      carrying_parts = allocate_parts(payload.chassis),
    }
    jobs[#jobs + 1] = job
    emit_receipt(intent, "accepted", nil, { parts = job.carrying_parts and 1 or 0 })
    if start == frame then
      job.started = true
      effect("factory_job_started", frame, factory.unit_id, id, job_detail(job))
    end
    return true
  end

  -- unit_created and factory_job_done are emitted only for a unit that exists.
  local function complete_job(job, frame)
    local unitID
    local factory_alive = factory.unit_id and Spring.ValidUnitID(factory.unit_id)
    if factory_alive then
      local fx, _, fz = Spring.GetUnitPosition(factory.unit_id)
      local x, z = fx + 32 * (job.seq % 4), fz + 96
      job_spawning = job
      unitID = Spring.CreateUnit(scenario.chassis[job.chassis].def, x, Spring.GetGroundHeight(x, z), z, 0, TEAM0)
      job_spawning = nil
    end
    if not unitID then
      local reason = factory_alive and "create_failed" or "factory_lost"
      effect("factory_job_failed", frame, factory.unit_id, job.intent_id, { job_id = job.job_id, reason = reason })
      return
    end
    embodied[unitID] = job.controller
    entity_names[unitID] = job.job_id
    if job.carrying_parts then carrying_parts[unitID] = true end
    apply_speed(unitID, 1, false)
    if principal_mode then
      for _, chip in ipairs(scenario.reasoner_chips) do
        if chip == job.controller then
          principals[job.job_id] = { chip = chip }
          principal_order[#principal_order + 1] = job.job_id
          table.sort(principal_order)
          unit_principal[unitID], principal_body_ids[unitID] = job.job_id, true
          delivered_reports[job.job_id] = {}
          break
        end
      end
    end
    local viewer = principal_for_unit(unitID)
    effect("unit_created", frame, unitID, job.intent_id, { controller = job.controller, job_id = job.job_id, chassis = job.chassis }, viewer)
    effect("factory_job_done", frame, factory.unit_id, job.intent_id, { job_id = job.job_id, unit_id = unitID }, principal_mode and "commander" or nil)
  end

  -- A predeployed asset debits one body and its chip whatever its state; a
  -- destroyed one is lost before the run and is not spawned.  An asset out of
  -- contact is a real team unit that is never reported to the team.
  local function spawn_asset(a, x, z)
    budgets.bodies_remaining = budgets.bodies_remaining - 1
    if a.controller then budgets.chips[a.controller] = budgets.chips[a.controller] - 1 end
    local ax, az = x + a.position[1], z + a.position[2]
    local record = { name = a.name, def = a.def, controller = a.controller, state = a.state, contact = a.contact, health = a.health,
                     speed_factor = a.speed_factor,
                     position = { ax, Spring.GetGroundHeight(ax, az), az } }
    assets[a.name] = record
    asset_order[#asset_order + 1] = a.name
    if a.state ~= "active" then initial_losses[a.name] = true end
    -- A loss the team witnessed before the run is legitimately known.
    record.known_lost = a.state ~= "active" and a.contact
    if a.state ~= "active" then return end
    spawning_hidden = not principal_mode and not a.contact
    local unitID = Spring.CreateUnit(a.def, ax, record.position[2], az, 0, TEAM0)
    spawning_hidden = false
    if not unitID then
      record.state = "spawn_failed"
      out("bridge_error", { reason = "predeployed_spawn_failed", asset = a.name })
      return
    end
    record.unit_id = unitID
    if a.controller then embodied[unitID] = a.controller end
    entity_names[unitID] = a.name
    if principal_mode and principals[a.name] then unit_principal[unitID], principal_body_ids[unitID] = a.name, true end
    if a.health then
      local _, max_health = Spring.GetUnitHealth(unitID)
      if max_health then Spring.SetUnitHealth(unitID, max_health * a.health) end
    end
    record.applied_max_speed = apply_speed(unitID, a.health or 1, false)
  end

  -- Evaluator-only truth, recorded before play: never part of an observation.
  local function world_init_record(frame)
    local truth, deployed, lost, impaired = {}, { bodies = commander and 1 or 0, chips = {} }, { bodies = 0, chips = {} }, false
    if commander then deployed.chips[commander.chip] = 1 end
    for i, name in ipairs(asset_order) do
      local a = assets[name]
      truth[i] = { name = name, def = a.def, controller = a.controller or Json.null, state = a.state, contact = a.contact,
                   health = a.health or Json.null, unit_id = a.unit_id or Json.null, position = a.position }
      impaired = impaired or a.health ~= nil
      local bucket = a.state == "active" and deployed or lost
      bucket.bodies = bucket.bodies + 1
      if a.controller then bucket.chips[a.controller] = (bucket.chips[a.controller] or 0) + 1 end
    end
    local inventory = { bodies = { total = scenario.bodies, deployed = deployed.bodies, lost = lost.bodies,
                                   available = budgets.bodies_remaining }, chips = {} }
    local consistent = scenario.bodies == deployed.bodies + lost.bodies + budgets.bodies_remaining
    for chip, total in pairs(scenario.chips) do
      local d, l = deployed.chips[chip] or 0, lost.chips[chip] or 0
      inventory.chips[chip] = { total = total, deployed = d, lost = l, available = budgets.chips[chip] }
      consistent = consistent and total == d + l + budgets.chips[chip]
    end
    local d = scenario.distress
    local link_check = {}
    if principal_mode then
      for i, name in ipairs(asset_order) do
        local a = assets[name]
        if a.unit_id then
          local computed = links[a.unit_id] == true
          link_check[#link_check + 1] = { name = name, expected = a.contact, computed = computed }
          if computed ~= a.contact then out("bridge_error", { reason = "contact_mismatch", asset = name, expected = a.contact, computed = computed }) end
        end
      end
    end
    local principal_bodies = {}
    if principal_mode then
      for _, name in ipairs(principal_order) do principal_bodies[name] = body_for_principal(name) or Json.null end
    end
    local speed_factors = { commander = scenario.commander.speed_factor or 1, chassis = {}, bodies = {} }
    for id, chassis in pairs(scenario.chassis) do speed_factors.chassis[id] = chassis.speed_factor or 1 end
    if commander and commander.unit_id then
      speed_factors.bodies.commander = { factor = speed_factor_for_unit(commander.unit_id), max_speed = apply_speed(commander.unit_id, 1, false) or Json.null }
    end
    local carried = 0
    for _, name in ipairs(asset_order) do
      local a = assets[name]
      if a.unit_id and carrying_parts[a.unit_id] then carried = carried + 1 end
      speed_factors.bodies[name] = { factor = a.speed_factor or (chassis_for_unit(a.unit_id) and scenario.chassis[chassis_for_unit(a.unit_id)].speed_factor) or 1,
                                     max_speed = a.applied_max_speed or Json.null }
    end
    local parts_total = scenario.resources and scenario.resources.parts or 0
    local parts_state = { total = parts_total, carried = carried, consumed = parts_consumed, available = resources and resources.parts or 0 }
    parts_state.consistent = parts_state.total == parts_state.carried + parts_state.consumed + parts_state.available
    local distress = Json.null
    if d then
      distress = { asset = d.asset, symptom = d.symptom, cause_hidden = d.cause_hidden,
                   observed_frame = reports[1] and reports[1].observed_frame }
      if scenario.entrapment and scenario.entrapment.beacon_fix then
        distress.fix = reports[1] and reports[1].fix or Json.null
      else
        distress.last_known = reports[1] and reports[1].position or Json.null
      end
    end
    return {
      name = "world_init", id = scenario.id, version = scenario.version, seed = scenario.seed or Json.null, frame = frame,
      commander = commander and { unit_id = commander.unit_id, chip = commander.chip, position = commander.position or Json.null } or Json.null,
      assets = truth, inventory = inventory, inventory_consistent = consistent,
      distress = distress,
      link_check = link_check,
      terrain_occlusion = terrain_method, impairment = impaired and "persistent" or Json.null,
      principal_bodies = principal_bodies, speed_factors = speed_factors,
      resources = resources and { parts = resources.parts } or Json.null,
      parts = parts_state,
    }
  end

  local function start_factory_scenario(name, x, z)
    local loaded, problem = load_scenario(name)
    if not loaded then
      -- Fail closed: no fixture and no hello, so the launcher reports the run
      -- as failed instead of running a mission the table does not describe.
      out("bridge_error", { reason = "scenario_invalid", detail = problem })
      scenario_failed = true
      return false
    end
    scenario = loaded
    scenario_origin_x, scenario_origin_z = x, z
    principal_mode, principals = loaded.principals ~= nil, loaded.principals or {}
    principal_order, unit_principal, links, delivered_reports, queued_reports = {}, {}, {}, {}, {}
    if principal_mode then principal_order = sorted_keys(principals) end
    -- Table positions are offsets from the map centre ([dx, dz] for the
    -- factory, [dx, ignored, dz] for waypoints); heights come from the ground.
    local w1 = scenario.waypoints.W1
    waypoint = { x + w1[1], Spring.GetGroundHeight(x + w1[1], z + w1[3]), z + w1[3] }
    waypoints_world = {}
    for name, w in pairs(scenario.waypoints) do
      if type(w) == "table" and type(w[1]) == "number" and type(w[3]) == "number" then
        waypoints_world[name] = { x + w[1], Spring.GetGroundHeight(x + w[1], z + w[3]), z + w[3] }
      end
    end
    budgets = { bodies_remaining = scenario.bodies, chips = {} }
    resources, parts_consumed = { parts = (scenario.resources and scenario.resources.parts) or 0 }, 0
    for id, n in pairs(scenario.chips) do budgets.chips[id] = n end
    out("scenario", { name = "factory_spawn", id = scenario.id, version = scenario.version, waypoints = { W1 = waypoint } })
    scenario_emitted = true
    -- The factory is infrastructure outside the mobile body cap.  No Loop 0
    -- smoke worker or Gaia probe is spawned in this scenario.
    local f = scenario.factory
    local fx, fz = x + f.offset[1], z + f.offset[2]
    factory = { name = f.name, def = f.def }
    factory.unit_id = Spring.CreateUnit(f.def, fx, Spring.GetGroundHeight(fx, fz), fz, 0, TEAM0)
    if factory.unit_id then entity_names[factory.unit_id] = f.name end
    if not factory.unit_id then out("bridge_error", { reason = "factory_spawn_failed" }) end
    for _, unitID in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do bind_commander(unitID) end
    -- UnitCreated may bind the stock commander while CreateUnit is still
    -- returning the factory id.  Place it once that id is definitely known.
    if commander and factory.unit_id and not commander.position then
      local fx, _, fz = Spring.GetUnitPosition(factory.unit_id)
      local offset = scenario.commander.position or { 0, 240 }
      if fx then
        Spring.SetUnitPosition(commander.unit_id, fx + offset[1], fz + offset[2])
        local cx, cy, cz = Spring.GetUnitPosition(commander.unit_id)
        commander.position = { cx, cy, cz }
        effect("commander_placed", Spring.GetGameFrame(), commander.unit_id, nil, { position = commander.position }, principal_mode and "commander" or nil)
      end
    end
    for _, a in ipairs(scenario.predeployed or {}) do spawn_asset(a, x, z) end
    local frame, d = Spring.GetGameFrame(), scenario.distress
    entrapment_state = nil
    if scenario.entrapment and scenario.entrapment["until"] == "parts_delivered" then
      local target = assets[scenario.entrapment.asset]
      if target and target.unit_id then
        entrapment_state = { config = scenario.entrapment, target = scenario.entrapment.asset,
                             unit_id = target.unit_id, delivered = false, frames = {},
                             beacon_only = scenario.entrapment.beacon_only == true,
                             coordinates_delivered = false }
        stop_unit(target.unit_id)
        apply_speed(target.unit_id, target.health or 1, true)
        effect("immobilised", frame, target.unit_id, nil,
               { target = scenario.entrapment.asset, unit_id = target.unit_id, reason = "needs_parts" },
               scenario.entrapment.asset)
      end
    end
    exposure_state = nil
    if scenario.exposure then
      local target = assets[scenario.exposure.asset]
      if target and target.unit_id then
        exposure_state = { config = scenario.exposure, target = scenario.exposure.asset, unit_id = target.unit_id,
                           cap = target.health or 1, critical = false, immobilised = false }
      end
    end
    recompute_links(frame)
    if d then
      -- A beacon-only table exposes one deliberately coarse fix, never a
      -- position or stale marker.  The cause stays evaluator-only.
      local report = { kind = "distress", asset = d.asset,
                       observed_frame = frame - d.last_known.age_frames, delivered_frame = frame,
                       age_frames = d.last_known.age_frames, symptom = d.symptom }
      local fix = scenario.entrapment and scenario.entrapment.beacon_fix
      local target = assets[d.asset]
      if fix and target and target.unit_id then
        local tx, _, tz = Spring.GetUnitPosition(target.unit_id)
        if fix.kind == "bearing" and commander and commander.unit_id and tx then
          local cx, _, cz = Spring.GetUnitPosition(commander.unit_id)
          if cx then report.fix = { kind = "bearing", from = "commander",
                                    bearing_deg = beacon_bearing(cx, cz, tx, tz, fix.error_deg, scenario.seed) } end
        elseif fix.kind == "quadrant" and tx then
          report.fix = { kind = "quadrant", cell_x = math.floor(tx / fix.cell_elmos),
                         cell_z = math.floor(tz / fix.cell_elmos), cell_elmos = fix.cell_elmos }
        end
      else
        local px, pz = x + d.last_known.position[1], z + d.last_known.position[2]
        report.position = { px, Spring.GetGroundHeight(px, pz), pz }
      end
      reports[1] = report
    end
    out("scenario", world_init_record(frame))
    return true
  end

  function gadget:GameStart()
    local frame = Spring.GetGameFrame()
    local x, z = Game.mapSizeX / 2, Game.mapSizeZ / 2
    local opts = Spring.GetModOptions() or {}
    local hello_scenario
    if opts.synapse_scenario and opts.synapse_scenario ~= "" then
      if not start_factory_scenario(opts.synapse_scenario, x, z) then return end
      hello_scenario = { id = scenario.id, version = scenario.version, seed = scenario.seed, waypoints = { W1 = waypoint } }
    else
      waypoint = { x + 400, Spring.GetGroundHeight(x + 400, z), z }
      -- Scenario is the citable cause and must be emitted before this gadget
      -- creates any fixture unit or emits a UnitCreated effect.
      out("scenario", { name = "smoke_spawn", waypoints = { W1 = waypoint } })
      scenario_emitted = true
      local y = Spring.GetGroundHeight(x, z)
      Spring.CreateUnit("armck", x, y, z, 0, TEAM0)
      local gaia = Spring.GetGaiaTeamID()
      -- Keep the visible foreign probe in LOS throughout the W1 advance rather
      -- than only at the starting position, while leaving the far probe hidden.
      Spring.CreateUnit("armck", x + 200, Spring.GetGroundHeight(x + 200, z + 100), z + 100, 0, gaia)
      Spring.CreateUnit("armck", x - 2500, Spring.GetGroundHeight(x - 2500, z), z, 0, gaia)
      hello_scenario = { waypoints = { W1 = waypoint } }
    end
    out("hello", {
      protocol = "synapse-mailbox/0",
      run_id = opts.synapse_run_id,
      game = Game.gameName,
      map = Game.mapName,
      engine = Game.version,
      frame_rate = 30,
      teams = { TEAM0 },
      clock_mode = "continuous",
      scenario = hello_scenario,
      principals = principal_mode and principal_order or nil,
      frame = frame,
    })
  end

  body_for_principal = function(principal)
    for unitID, owner in pairs(unit_principal) do
      if owner == principal and Spring.ValidUnitID(unitID) then return unitID end
    end
    return nil
  end

  local function has_capability(unitID, verb)
    if not principal_mode then return true end
    local capabilities
    if commander and commander.unit_id == unitID then
      capabilities = scenario.commander.capabilities or { "move", "sense", "assist" }
    else
      local chassis = chassis_for_unit(unitID)
      local defaults = { scout_v1 = { "move", "sense" }, carrier_v1 = { "move", "assist" }, relay_support_v1 = { "move", "relay", "sense" } }
      capabilities = chassis and (scenario.chassis[chassis].capabilities or defaults[chassis]) or {}
    end
    for _, capability in ipairs(capabilities) do
      if capability == verb then return true end
    end
    return false
  end

  local function queue_report(record)
    queued_reports[#queued_reports + 1] = record
  end

  local function remember_report(principal, body)
    local list = delivered_reports[principal] or {}
    delivered_reports[principal] = list
    list[#list + 1] = body
    while #list > MAX_REPORTS do table.remove(list, 1) end
  end

  local function schedule_status_and_detections(frame)
    if not principal_mode then return end
    for _, source in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do
      local target_principal = principal_for_unit(source)
      if embodied[source] and target_principal and links[source] then
        local observed = { kind = "status", from = entity_names[source] or target_principal,
                           observed_frame = frame, body = entity_for(source, "sensor") }
        queue_report({ due = frame + scenario.comms.latency_frames, from = source, to = target_principal, body = observed })
        for _, candidate in ipairs(Spring.GetAllUnits()) do
          if candidate ~= source and not beacon_isolated(candidate) and detects(source, candidate) then
            queue_report({ due = frame + scenario.comms.latency_frames, from = source, to = target_principal,
                           body = { kind = "detection", from = entity_names[source] or target_principal,
                                    observed_frame = frame, entity = entity_for(candidate, "sensor") } })
          end
        end
      end
    end
  end

  local function deliver_due_reports(frame)
    if not principal_mode then return end
    local remaining = {}
    for _, report in ipairs(queued_reports) do
      if report.due > frame then
        remaining[#remaining + 1] = report
      elseif report.message then
        local sender, recipient = body_for_principal(report.from), body_for_principal(report.to)
        if recipient and beacon_isolated(recipient) then
          effect("message_dropped", frame, sender, report.intent_id, { reason = "comms_destroyed", to = report.to }, report.from)
        elseif sender and recipient and links[sender] and links[recipient] then
          local body = { kind = "message", from = report.from, text = report.text, observed_frame = report.observed_frame,
                         delivered_frame = frame, age_frames = frame - report.observed_frame }
          remember_report(report.to, body)
          if entrapment_state and entrapment_state.delivered and entrapment_state.target == report.to then
            local px, pz = report.text:match("^EXTRACT%s+(-?%d+)%s+(-?%d+)%s*$")
            px, pz = tonumber(px), tonumber(pz)
            if px and pz and px >= 0 and pz >= 0 and px <= Game.mapSizeX and pz <= Game.mapSizeZ then
              entrapment_state.coordinates_delivered = true
            end
          end
          effect("message_delivered", frame, recipient, report.intent_id, { to = report.to }, report.to)
          effect("message_delivered", frame, sender, report.intent_id, { to = report.to }, report.from)
        else
          effect("message_dropped", frame, sender, report.intent_id, { reason = "no_link", to = report.to }, report.from)
        end
      elseif links[report.from] then
        local body = report.body
        body.delivered_frame, body.age_frames = frame, frame - body.observed_frame
        remember_report(report.to, body)
      else
        effect("report_dropped", frame, report.from, nil, { reason = "no_link", from = report.body.from }, report.to)
      end
    end
    queued_reports = remaining
  end

  local function outcome(frame, result, target)
    if mission_outcome then return end
    mission_outcome = result
    local before, during, got = {}, {}, {}
    for _, name in ipairs(asset_order) do
      local a = assets[name]
      if initial_losses[name] then before[#before + 1] = name end
      if lost[name] and not initial_losses[name] then during[#during + 1] = name end
      if extracted[name] then got[#got + 1] = name end
    end
    local detail = { result = result, frame = frame, target = target, losses_before_run = before,
                     losses_during_run = during, extracted = got, bodies_remaining = budgets and budgets.bodies_remaining,
                     chips_remaining = budgets and budgets.chips }
    if commander and commander.unit_id then effect("mission_outcome", frame, commander.unit_id, nil, detail, "commander") end
    out("scenario", { name = "mission_outcome", result = result, frame = frame, target = target,
                        losses_before_run = before, losses_during_run = during, extracted = got,
                        bodies_remaining = budgets and budgets.bodies_remaining, chips_remaining = budgets and budgets.chips })
  end

  local function update_rescue_state(frame)
    if not (principal_mode and scenario and scenario.objective and scenario.extraction_zones) then return end
    local target = scenario.objective.target
    local zone = scenario.extraction_zones[scenario.objective.extraction]
    local unitID = target and body_for_principal(target)
    if not target or not zone then return end
    if not unitID then outcome(frame, "no_target", target); return end
    if extracted[target] or lost[target] then return end
    if scenario.mission and frame >= (scenario.mission.timeout_frames or 12000) then outcome(frame, "timeout", target); return end
    local x, _, z = Spring.GetUnitPosition(unitID)
    local zx, zz = scenario_origin_x + zone.position[1], scenario_origin_z + zone.position[2]
    if x and squared_distance(x, z, zx, zz) <= zone.radius * zone.radius then
      extraction_dwell[target] = (extraction_dwell[target] or 0) + 1
      if extraction_dwell[target] >= zone.dwell_frames then
        extracted[target] = true
        local detail = { target = target, unit_id = unitID, custody = "friendly" }
        effect("mind_extracted", frame, unitID, nil, detail, target)
        effect("mind_extracted", frame, unitID, nil, detail, "commander")
        Spring.DestroyUnit(unitID, false, true)
        outcome(frame, "extracted", target)
      end
    else
      extraction_dwell[target] = 0
    end
  end

  local function update_entrapment_state(frame)
    local state = entrapment_state
    if not state or state.delivered or not Spring.ValidUnitID(state.unit_id) then return end
    local target_x, _, target_z = Spring.GetUnitPosition(state.unit_id)
    if not target_x then return end
    local eligible, selected = {}, nil
    for _, unitID in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do
      local chassis, allowed = chassis_for_unit(unitID), false
      for _, id in ipairs(state.config.parts_carrier_chassis) do if chassis == id then allowed = true; break end end
      if unitID ~= state.unit_id and allowed and embodied[unitID] and carrying_parts[unitID]
        and has_capability(unitID, state.config.assist_capability) then
        eligible[#eligible + 1] = unitID
      else
        state.frames[unitID] = nil
      end
    end
    table.sort(eligible)
    for _, unitID in ipairs(eligible) do
      local x, _, z = Spring.GetUnitPosition(unitID)
      if x and squared_distance(x, z, target_x, target_z) <= state.config.assist_range * state.config.assist_range then
        state.frames[unitID] = (state.frames[unitID] or 0) + 1
        if state.frames[unitID] >= state.config.assist_frames and not selected then selected = unitID end
      else
        state.frames[unitID] = 0
      end
    end
    if not selected then return end
    state.delivered = true
    carrying_parts[selected] = nil
    parts_consumed = parts_consumed + 1
    local target_name, asset = asset_for_unit(state.unit_id)
    apply_speed(state.unit_id, asset and asset.health or 1, false)
    local by = entity_names[selected] or ("unit-" .. selected)
    local detail = { target = state.target, by = by, unit_id = state.unit_id }
    effect_to_target_viewers("parts_delivered", frame, state.target, state.unit_id, detail)
    effect_to_target_viewers("mobility_restored", frame, state.target, state.unit_id, { target = state.target })
    if state.beacon_only then
      effect_to_target_viewers("comms_restored", frame, state.target, state.unit_id, { target = state.target })
    end
  end

  local function update_exposure_state(frame)
    local state = exposure_state
    if not state or not Spring.ValidUnitID(state.unit_id) then return end
    if frame % OBS_INTERVAL ~= 0 then return end
    local asset = assets[state.target]
    if not asset or extracted[state.target] or lost[state.target] then return end
    state.cap = math.max(state.config.floor, state.cap - state.config.decay_per_minute * OBS_INTERVAL / (60 * 30))
    if not state.critical and state.cap < 0.25 then
      state.critical = true
      effect_to_target_viewers("exposure_critical", frame, state.target, state.unit_id, { target = state.target, unit_id = state.unit_id })
    end
    if state.cap <= state.config.floor then
      state.immobilised = true
      stop_unit(state.unit_id)
    end
  end

  -- Principal-mode rejection must not turn an arbitrary numeric id into an
  -- existence oracle.  A principal may name its own body, a body it currently
  -- detects, or (for the commander) a named principal body that is already
  -- exposed by the in-contact roster.  Static authority is deliberately
  -- checked only after this visibility boundary.
  local function principal_can_name_unit(principal, unitID)
    local bodyID = body_for_principal(principal)
    if not bodyID or not Spring.ValidUnitID(bodyID) then return false end
    if unitID == bodyID or detects(bodyID, unitID) then return true end
    return principal == "commander" and Spring.GetUnitTeam(unitID) == TEAM0 and links[unitID] == true
  end

  function gadget:RecvLuaMsg(msg, playerID)
    if msg:sub(1, #PREFIX) ~= PREFIX then return false end
    -- MAX_LINE bounds the JSONL record; the internal SYN0 prefix is not part
    -- of that external record and must not reject a valid 4096-byte line.
    if #msg > MAX_LINE + #PREFIX then
      out("bridge_error", { reason = "too_large", bytes = #msg })
      return true
    end
    local ok, record = pcall(Json.decode, msg:sub(#PREFIX + 1))
    if not ok or type(record) ~= "table" or not valid_tree(record, 1) then
      emit_receipt(nil, "rejected", "schema_invalid")
      return true
    end
    if record.kind == "control" then
      if record.op == "quit" then
        out("bye", { reason = "quit", frame = Spring.GetGameFrame() })
        SendToUnsynced("synapse_quit")
      end
      return true
    end
    local intent = record.envelope
    if type(intent) ~= "table" or intent.schema ~= "IntentEnvelope/v1" or type(intent.payload) ~= "table" then
      emit_receipt(intent, "rejected", "schema_invalid")
      return true
    end
    local id = intent.intent_id
    if type(id) ~= "string" or #id == 0 then
      emit_receipt(intent, "rejected", "schema_invalid")
      return true
    end
    if seen[id] then
      -- A reused id is a distinct rejected attempt; never replay an accepted
      -- receipt, because that would misrepresent its causal outcome.
      emit_receipt(intent, "rejected", "duplicate_intent_id")
      return true
    end
    seen[id] = true
    if scenario_failed then emit_receipt(intent, "rejected", "scenario_invalid"); return true end
    if has_raw_engine(intent, 1) then emit_receipt(intent, "rejected", "raw_engine_forbidden"); return true end
    local _, _, spectator, teamID = Spring.GetPlayerInfo(playerID, false)
    if spectator or tonumber(intent.team) ~= teamID then emit_receipt(intent, "rejected", "wrong_team"); return true end
    -- Every intent comes from the embodied commander; with no living
    -- commander body the team has no principal able to act.
    if scenario and not (commander and Spring.ValidUnitID(commander.unit_id)) then
      emit_receipt(intent, "rejected", "principal_unembodied"); return true
    end
    local principal = intent.principal
    if principal_mode and (type(principal) ~= "string" or not principals[principal] or not body_for_principal(principal)) then
      emit_receipt(intent, "rejected", "unknown_principal"); return true
    end
    if scenario and intent.type == "PRODUCE" then return reduce_produce(intent, id, teamID) end
    if principal_mode and intent.type == "MESSAGE" then
      local payload, frame = intent.payload, Spring.GetGameFrame()
      if type(payload.to) ~= "string" or not principals[payload.to] then emit_receipt(intent, "rejected", "unknown_principal"); return true end
      if type(payload.text) ~= "string" or #payload.text > 256 then emit_receipt(intent, "rejected", "invalid_payload"); return true end
      if beacon_principal(principal) then emit_receipt(intent, "rejected", "comms_destroyed"); return true end
      if not use_token(principal, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
      emit_receipt(intent, "accepted", nil)
      effect("message_sent", frame, body_for_principal(principal), id, { to = payload.to }, principal)
      queue_report({ due = frame + scenario.comms.latency_frames, message = true, from = principal, to = payload.to,
                     text = payload.text, observed_frame = frame, intent_id = id })
      return true
    end
    if intent.type ~= "TASK" then emit_receipt(intent, "rejected", "unsupported_type"); return true end
    local payload = intent.payload
    if type(payload.args) ~= "table" then
      emit_receipt(intent, "rejected", "verb_not_supported"); return true
    end
    local target = waypoint
    if payload.verb == "advance" then
      if next(payload.args) ~= nil then emit_receipt(intent, "rejected", "verb_not_supported"); return true end
    elseif payload.verb == "move_to" and scenario and scenario.extraction_zones then
      local tx, tz = payload.args.x, payload.args.z
      if type(tx) ~= "number" or type(tz) ~= "number" or tx % 1 ~= 0 or tz % 1 ~= 0
        or tx < 0 or tz < 0 or tx > Game.mapSizeX or tz > Game.mapSizeZ then
        emit_receipt(intent, "rejected", "move_target_invalid"); return true
      end
      target = { tx, Spring.GetGroundHeight(tx, tz), tz }
      -- A late order for a body that is already extracted or destroyed must be
      -- refused like any id outside the sender's view (live-12: TestMoveOrder was
      -- handed a nil definition and the gadget faulted).
      local moverID = tonumber(payload.unit or payload.unit_id)
      local moverDef = moverID and Spring.ValidUnitID(moverID) and Spring.GetUnitDefID(moverID) or nil
      if not moverDef then
        emit_receipt(intent, "rejected", "unknown_unit"); return true
      end
      if Spring.TestMoveOrder and not Spring.TestMoveOrder(moverDef, tx, target[2], tz) then
        emit_receipt(intent, "rejected", "move_target_invalid"); return true
      end
    else
      emit_receipt(intent, "rejected", "verb_not_supported"); return true
    end
    local unitID = tonumber(payload.unit or payload.unit_id)
    -- A unit out of contact answers exactly like a missing one, so probing ids
    -- cannot confirm that it exists.
    if not unitID or unitID % 1 ~= 0 or not Spring.ValidUnitID(unitID) then
      emit_receipt(intent, "rejected", "unknown_unit"); return true
    end
    if principal_mode and not principal_can_name_unit(principal, unitID) then
      emit_receipt(intent, "rejected", "unknown_unit"); return true
    end
    if Spring.GetUnitTeam(unitID) ~= teamID then emit_receipt(intent, "rejected", "not_owned"); return true end
    -- The factory and any unbudgeted unit are not embodiments and take no orders.
    if scenario and not embodied[unitID] then emit_receipt(intent, "rejected", "not_embodied"); return true end
    if principal_mode then
      local owner = unit_principal[unitID]
      if owner and owner ~= principal then emit_receipt(intent, "rejected", "not_authorized"); return true end
      if not owner and principal ~= "commander" then emit_receipt(intent, "rejected", "not_authorized"); return true end
      if not owner and not links[unitID] then emit_receipt(intent, "rejected", "no_link"); return true end
      if not has_capability(unitID, "move") then emit_receipt(intent, "rejected", "incapable"); return true end
    end
    if (entrapment_state and not entrapment_state.delivered and entrapment_state.unit_id == unitID)
      or (exposure_state and exposure_state.immobilised and exposure_state.unit_id == unitID) then
      stop_unit(unitID)
      emit_receipt(intent, "rejected", "immobilised"); return true
    end
    if payload.verb == "move_to" and entrapment_state and entrapment_state.beacon_only
      and entrapment_state.unit_id == unitID and not entrapment_state.coordinates_delivered then
      emit_receipt(intent, "rejected", "no_position"); return true
    end
    local frame = Spring.GetGameFrame()
    local problem = freshness_problem(intent, principal_mode and principal or teamID, frame)
    if problem then emit_receipt(intent, "rejected", problem); return true end
    local x, _, z = Spring.GetUnitPosition(unitID)
    if x and ((x - target[1]) * (x - target[1]) + (z - target[3]) * (z - target[3]) <= 32 * 32) then
      emit_receipt(intent, "rejected", "already_at_waypoint"); return true
    end
    if pending[unitID] then emit_receipt(intent, "rejected", "unit_busy"); return true end
    if not use_token(principal_mode and principal or teamID, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
    pending[unitID] = { intent_id = id, last_x = x, last_z = z, principal = principal, target = target, progress_frame = frame,
                        arrival_radius = payload.verb == "advance" and 16 or 50 }
    Spring.GiveOrderToUnit(unitID, CMD.MOVE, target, 0)
    emit_receipt(intent, "accepted", nil)
    effect("order_started", frame, unitID, id, { target = target }, principal_mode and principal or nil)
    return true
  end

  function gadget:GameFrame(frame)
    recompute_links(frame)
    deliver_due_reports(frame)
    update_exposure_state(frame)
    if scenario then
      for _, name in ipairs(asset_order) do
        local a = assets[name]
        if a.health and a.state == "active" and not extracted[name] and not lost[name]
          and a.unit_id and Spring.ValidUnitID(a.unit_id) then
          local health, max_health = Spring.GetUnitHealth(a.unit_id)
          local health_factor = health_factor_for_asset(a)
          if health and max_health and health > max_health * health_factor then Spring.SetUnitHealth(a.unit_id, max_health * health_factor) end
          a.applied_max_speed = apply_speed(a.unit_id, health_factor, held_for_unit(a.unit_id))
        end
      end
    end
    if scenario and #jobs > 0 then
      local remaining = {}
      for _, job in ipairs(jobs) do
        if not job.started and frame >= job.started_frame then
          job.started = true
          effect("factory_job_started", frame, factory.unit_id, job.intent_id, job_detail(job), principal_mode and "commander" or nil)
        end
        if job.started and frame >= job.done_frame then
          complete_job(job, frame)
        else
          remaining[#remaining + 1] = job
        end
      end
      jobs = remaining
    end
    for unitID, command in pairs(pending) do
      local x, _, z = Spring.GetUnitPosition(unitID)
      if Spring.GetUnitCommandCount(unitID) == 0 then
        effect("order_done", frame, unitID, command.intent_id, { reason = "queue_empty" }, command.principal)
        pending[unitID] = nil
      elseif x and ((x - command.target[1]) * (x - command.target[1]) + (z - command.target[3]) * (z - command.target[3]) <= command.arrival_radius * command.arrival_radius) then
        -- Reissuing the fixed move while already at W1 may not produce an
        -- engine UnitCmdDone callback. Physical arrival is still terminal.
        effect("order_done", frame, unitID, command.intent_id, { reason = "arrived" }, command.principal)
        pending[unitID] = nil
      elseif x then
        -- Progress is any displacement of at least one elmo since the last mark;
        -- unit_moved is still reported only every 32 elmos so a moving body does
        -- not flood the outbox once per frame.
        local mx, mz = x - (command.mark_x or command.last_x), z - (command.mark_z or command.last_z)
        if mx * mx + mz * mz >= 1 then command.mark_x, command.mark_z, command.progress_frame = x, z, frame end
        local dx, dz = x - command.last_x, z - command.last_z
        if dx * dx + dz * dz >= 32 * 32 then
          command.last_x, command.last_z = x, z
          effect("unit_moved", frame, unitID, command.intent_id, { pos = { x, z } }, command.principal)
        elseif frame - command.progress_frame >= 150 then
          effect("order_done", frame, unitID, command.intent_id, { reason = "no_progress" }, command.principal)
          pending[unitID] = nil
        end
      end
    end
    update_entrapment_state(frame)
    update_rescue_state(frame)
    if frame % OBS_INTERVAL ~= 0 then return end
    if principal_mode then
      schedule_status_and_detections(frame)
      for _, principal in ipairs(principal_order) do
        local bodyID = body_for_principal(principal)
        if bodyID then
          local isolated = beacon_principal(principal)
          local own_body = entity_for(bodyID, "own_body")
          if isolated and own_body then own_body.pos = nil end
          local entities = { own_body }
          for _, unitID in ipairs(Spring.GetAllUnits()) do
            local authority_body = principal == "commander" and unitID ~= bodyID and Spring.GetUnitTeam(unitID) == TEAM0
              and embodied[unitID] and not unit_principal[unitID] and links[unitID] == true
            local prohibited = isolated and ((commander and unitID == commander.unit_id) or (factory and unitID == factory.unit_id))
            if unitID ~= bodyID and not prohibited and (authority_body or detects(bodyID, unitID)) then
              local entity = isolated and relative_entity_for(bodyID, unitID, authority_body and "own_unit" or "sensor")
                or entity_for(unitID, authority_body and "own_unit" or "sensor")
              if entity then entities[#entities + 1] = entity end
            end
          end
          table.sort(entities, function(a, b) return a.id < b.id end)
          local listed_reports = {}
          for i, report in ipairs(delivered_reports[principal] or {}) do listed_reports[i] = report end
          local body = { schema = "ObservationFrame/v1", principal = principal,
                         observation_id = "obs-" .. principal .. "-" .. frame, team = TEAM0, frame = frame,
                         body = own_body, entities = entities,
                         reports = listed_reports, truncated = false, omitted = 0,
                         scenario = { id = scenario.id, version = scenario.version },
                         permitted_intents = { { type = "TASK", verbs = { "advance", "move_to" } }, { type = "MESSAGE" } } }
          -- Rescue-party knowledge only: the commander and the minds it produced
          -- (job-N) know the planned waypoints and the extraction zone; a stranded
          -- principal is cut off and learns the extraction point only from a
          -- delivered MESSAGE (18:20 ruling after live-05, where M1 walked home
          -- from its own view without any contact).
          if principal == "commander" or principal:sub(1, 4) == "job-" then
            body.scenario.waypoints = next(waypoints_world) and waypoints_world or { W1 = waypoint }
            if scenario.objective and scenario.extraction_zones then
              local zone = scenario.extraction_zones[scenario.objective.extraction]
              if zone then body.scenario.extraction = { id = scenario.objective.extraction,
                                                         position = { scenario_origin_x + zone.position[1], scenario_origin_z + zone.position[2] },
                                                         radius = zone.radius } end
            end
          end
          if principal == "commander" then
            body.budgets = { bodies_remaining = budgets.bodies_remaining, chips = budgets.chips }
            body.resources = { parts = resources.parts }
            body.permitted_intents[#body.permitted_intents + 1] = {
              type = "PRODUCE", factories = { factory.name },
              chassis = sorted_keys(scenario.chassis), controllers = sorted_keys(scenario.chips),
            }
            if scenario.objective then
              local target = scenario.objective.target
              body.objective = { type = scenario.objective.type, target = target,
                                 status = extracted[target] and "extracted" or (lost[target] and "lost" or (mission_outcome == "timeout" and "timeout" or "pending")) }
            end
            body.factories = { { name = factory.name, unit_id = factory.unit_id or Json.null } }
            body.link_state, body.roster = {}, {}
            for _, unitID in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do
              if not principal_body_ids[unitID] then body.link_state[#body.link_state + 1] = { unit_id = unitID, linked = links[unitID] == true } end
            end
            for _, name in ipairs(asset_order) do
              local a = assets[name]
              local linked = a.unit_id and links[a.unit_id] == true
              local status = extracted[name] and "extracted" or (lost[name] and "lost" or (linked and "in_contact" or "no_contact"))
              local entry = { name = name, def = a.def, controller = a.controller or Json.null,
                              status = status, unit_id = status == "in_contact" and a.unit_id or nil }
              if status == "in_contact" then entry.carrying_parts = carrying_parts[a.unit_id] == true end
              body.roster[#body.roster + 1] = entry
            end
            if #reports > 0 then body.reports = body.reports or {}; for _, report in ipairs(reports) do table.insert(body.reports, report) end end
          end
          while #Json.encode(body) > 32768 and #body.entities > 1 do table.remove(body.entities); body.truncated = true; body.omitted = body.omitted + 1 end
          while #Json.encode(body) > 32768 and #body.reports > 0 do
            local remove = 1
            for i, report in ipairs(body.reports) do if report.kind ~= "message" then remove = i; break end end
            table.remove(body.reports, remove); body.truncated = true; body.omitted = body.omitted + 1
          end
          remember_observation(principal, body.observation_id, frame)
          out("observation", body)
        end
      end
      return
    end
    local entities = {}
    for _, unitID in ipairs(Spring.GetAllUnits()) do
      local teamID = Spring.GetUnitTeam(unitID)
      local _, _, _, _, _, allyTeam = Spring.GetTeamInfo(TEAM0, false)
      local source = legacy_visible_source(unitID, teamID, allyTeam)
      if source then
        local x, y, z = Spring.GetUnitPosition(unitID)
        if x then
          local definition = UnitDefs[Spring.GetUnitDefID(unitID)]
          local entity = { id = unitID, team = teamID, pos = { x, y, z }, source = source, def = definition and definition.name }
          if source == "own_unit" then
            entity.health = Spring.GetUnitHealth(unitID)
            entity.commands = Spring.GetUnitCommandCount(unitID)
            entity.controller = embodied[unitID]
          end
          entities[#entities + 1] = entity
        end
      end
    end
    table.sort(entities, function(a, b) return a.id < b.id end)
    while #entities > 32 do table.remove(entities) end
    local id = "obs-0-" .. frame
    local body = { schema = "ObservationFrame/v1", observation_id = id, team = TEAM0, frame = frame, entities = entities, scenario = { waypoints = { W1 = waypoint } }, permitted_intents = { { type = "TASK", verbs = { "advance" } } }, truncated = false, omitted = 0 }
    if scenario then
      -- Every supported choice stays listed at zero stock, so an exhausted
      -- one reaches this reducer and is refused with its budget reason.
      body.permitted_intents[2] = {
        type = "PRODUCE", factories = { factory.name },
        chassis = sorted_keys(scenario.chassis), controllers = sorted_keys(scenario.chips),
      }
      body.scenario = { id = scenario.id, version = scenario.version, waypoints = { W1 = waypoint } }
      body.commander = commander and { unit_id = commander.unit_id, chip = commander.chip } or Json.null
      local chips = {}
      for chip, n in pairs(budgets.chips) do chips[chip] = n end
      body.budgets = { bodies_remaining = budgets.bodies_remaining, chips = chips }
      local listed = {}
      for i, job in ipairs(jobs) do
        listed[i] = { job_id = job.job_id, intent_id = job.intent_id, chassis = job.chassis, controller = job.controller,
                      started_frame = job.started_frame, done_frame = job.done_frame }
      end
      body.factories = { { name = factory.name, unit_id = factory.unit_id or Json.null, jobs = listed } }
      if #asset_order > 0 then
        -- What the team legitimately knows about its own deployed assets: their
        -- identity, and contact or an observed loss, never their hidden state.
        local roster = {}
        for i, name in ipairs(asset_order) do
          local a = assets[name]
          local live = a.unit_id and Spring.ValidUnitID(a.unit_id)
          local status = live and "in_contact" or (a.known_lost and "lost" or "no_contact")
          roster[i] = { name = name, def = a.def, controller = a.controller or Json.null, status = status,
                        unit_id = live and a.unit_id or nil }
        end
        body.roster = roster
      end
      if #reports > 0 then
        local listed_reports = {}
        for i, r in ipairs(reports) do
          listed_reports[i] = { kind = r.kind, asset = r.asset, position = r.position, symptom = r.symptom,
                                observed_frame = r.observed_frame, delivered_frame = r.delivered_frame,
                                age_frames = r.age_frames or (frame - r.observed_frame) }
        end
        body.reports = listed_reports
      end
    end
    while #Json.encode(body) > 32768 and #entities > 0 do table.remove(entities); body.truncated = true; body.omitted = body.omitted + 1 end
    remember_observation(TEAM0, id, frame)
    out("observation", body)
  end

  function gadget:UnitCreated(unitID, _, teamID)
    -- A factory job reports its own unit_created carrying the controller id.
    if job_spawning then return end
    -- An out-of-contact legacy asset is evaluator state, not an observation or
    -- an effect, until the scenario explicitly reports it.
    if spawning_hidden then hidden[unitID] = true; return end
    if scenario_emitted and can_emit_effect(unitID, teamID) then out("effect", { schema = "EffectEvent/v1", effect = "unit_created", frame = Spring.GetGameFrame(), unit_id = unitID, team = teamID }) end
    bind_commander(unitID)
  end

  function gadget:UnitCmdDone(unitID, _, teamID, cmdID)
    local command = pending[unitID]
    if command and teamID == TEAM0 and cmdID == CMD.MOVE then
      effect("order_done", Spring.GetGameFrame(), unitID, command.intent_id, nil, command.principal)
      pending[unitID] = nil
    end
  end

  function gadget:UnitDestroyed(unitID, _, teamID)
    local command = pending[unitID]
    local owner = principal_for_unit(unitID)
    if principal_mode and owner and not extracted[owner] and not lost[owner] then
      lost[owner] = true
      local detail = { target = owner, unit_id = unitID }
      effect("mind_lost", Spring.GetGameFrame(), unitID, command and command.intent_id, detail, owner)
      if owner ~= "commander" then effect("mind_lost", Spring.GetGameFrame(), unitID, command and command.intent_id, detail, "commander") end
      if scenario and scenario.objective and scenario.objective.target == owner then outcome(Spring.GetGameFrame(), "lost", owner) end
    end
    if principal_mode then
      if owner then effect("unit_destroyed", Spring.GetGameFrame(), unitID, command and command.intent_id, nil, owner) end
    elseif can_emit_effect(unitID, teamID) then
      effect("unit_destroyed", Spring.GetGameFrame(), unitID, command and command.intent_id)
    end
    pending[unitID] = nil
    embodied[unitID] = nil
    for _, name in ipairs(asset_order) do
      local a = assets[name]
      if a.unit_id == unitID then a.state, a.known_lost = "destroyed", true end
    end
    unit_principal[unitID], entity_names[unitID], links[unitID] = nil, nil, nil
    if commander and commander.unit_id == unitID then commander, commander_lost = nil, true end
    if factory and factory.unit_id == unitID then factory.unit_id = nil end
  end
else
  local INBOX, OUTBOX = "synapse/inbox.jsonl", "synapse/outbox.jsonl"
  local offset, partial, seq, timer, quit, discarding = 0, "", 0, nil, false, false

  local function write(kind, body)
    seq = seq + 1
    local engine_ms = math.floor(Spring.DiffTimers(Spring.GetTimer(), timer, true))
    local file = io.open(OUTBOX, "a")
    if not file then return end
    file:write(string.format('{"seq":%d,"frame":%d,"engine_ms":%d,"kind":"%s","body":%s}\n', seq, Spring.GetGameFrame(), engine_ms, kind, body))
    file:close()
  end

  local function on_output(_, kind, body) write(kind, body) end
  local function on_quit() quit = true end

  function gadget:Initialize()
    timer = Spring.GetTimer()
    gadgetHandler:AddSyncAction("synapse_out", on_output)
    gadgetHandler:AddSyncAction("synapse_quit", on_quit)
  end

  function gadget:Shutdown()
    gadgetHandler:RemoveSyncAction("synapse_out")
    gadgetHandler:RemoveSyncAction("synapse_quit")
  end

  function gadget:Update()
    if quit then quit = false; Spring.SendCommands("quitforce"); return end
    -- Drain already-complete buffered lines even when no later append wakes us.
    -- This matters after the per-Update eight-record forwarding cap.
    local chunk = ""
    local file = io.open(INBOX, "rb")
    if file then
      file:seek("set", offset)
      chunk = file:read(65536) or ""
      file:close()
      offset = offset + #chunk
    end
    if #partial == 0 and #chunk == 0 then return end
    local buffer, forwarded = partial .. chunk, 0
    if discarding then
      local newline = buffer:find("\n", 1, true)
      if not newline then partial = ""; return end
      buffer, discarding = buffer:sub(newline + 1), false
    end
    while forwarded < 8 do
      local newline = buffer:find("\n", 1, true)
      if not newline then
        if #buffer > MAX_LINE then
          write("bridge_error", string.format('{"reason":"too_large","bytes":%d}', #buffer))
          buffer, discarding = "", true
        end
        break
      end
      local line = buffer:sub(1, newline - 1)
      buffer = buffer:sub(newline + 1)
      if #line > MAX_LINE then
        write("bridge_error", string.format('{"reason":"too_large","bytes":%d}', #line))
      elseif #line > 0 then
        Spring.SendLuaRulesMsg(PREFIX .. line)
        forwarded = forwarded + 1
      end
    end
    partial = buffer
  end
end
