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
  local TEAM0, OBS_INTERVAL, MAX_OBSERVATIONS = 0, 15, 128
  local observations, observation_order, seen, pending, rate = {}, {}, {}, {}, {}
  local waypoint, scenario_emitted = nil, false
  -- Factory provisioning (rah26-8zp), active only when the synapse_scenario
  -- mod option names a ScenarioTable/v1 file in this archive.  Every count
  -- and unit name comes from that versioned table, never from this code.
  local SCENARIO_LIMIT, MAX_JOBS = 16384, 8
  local scenario, budgets, commander, factory = nil, nil, nil, nil
  local jobs, job_seq, job_spawning, embodied, commander_lost = {}, 0, nil, {}, false
  -- Initial world (rah26-5hf). Assets hold evaluator truth; principal-mode
  -- observations derive only from each body and delivered reports.
  local assets, asset_order, reports = {}, {}, {}
  -- Per-principal state is enabled only by a declared principals table.
  -- In that mode observations are calculated from geometry below, never from
  -- team LOS or an engine sensor-radius mutation.
  local principal_mode, principals, principal_order, unit_principal = false, {}, {}, {}
  local links, delivered_reports, queued_reports, entity_names = {}, {}, {}, {}
  local local_status, factory_name = {}, "F0"
  local effect, body_for_principal
  local scenario_failed = false

  local function identifier(value)
    return type(value) == "string" and #value > 0 and #value <= 64 and value:match("^[%w_.-]+$") ~= nil
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
      for _, verb in ipairs(c.capabilities or {}) do if verb ~= "move" and verb ~= "sense" and verb ~= "assist" and verb ~= "relay" then return nil, "invalid_chassis" end end
      kinds = kinds + 1
    end
    if kinds < 1 or kinds > 8 then return nil, "invalid_chassis" end
    local w1 = type(t.waypoints) == "table" and t.waypoints.W1
    if type(w1) ~= "table" or type(w1[1]) ~= "number" or type(w1[3]) ~= "number" then return nil, "invalid_waypoint" end
    -- Optional initial-world fields.  The table is the fully chosen world for
    -- its (id, version, seed): the gadget draws no randomness of its own.
    if t.seed ~= nil and not count(t.seed) then return nil, "invalid_seed" end
    local names, bodies_needed, chips_needed = {}, 1, { [t.commander.chip] = 1 }
    if t.predeployed ~= nil then
      if type(t.predeployed) ~= "table" or #t.predeployed > 8 then return nil, "invalid_predeployed" end
      for _, a in ipairs(t.predeployed) do
        if type(a) == "table" and a.controller == Json.null then a.controller = nil end
        if type(a) ~= "table" or not identifier(a.name) or names[a.name] or not identifier(a.def) or not UnitDefNames[a.def]
          or (a.controller ~= nil and (not identifier(a.controller) or t.chips[a.controller] == nil))
          or not offset_pair(a.position) or (a.state ~= "active" and a.state ~= "destroyed") or type(a.contact) ~= "boolean"
          or (a.health ~= nil and (type(a.health) ~= "number" or a.health <= 0 or a.health > 1)) then
          return nil, "invalid_predeployed"
        end
        names[a.name] = true
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
        or not count(d.last_known.age_frames) or not identifier(d.symptom) or not identifier(d.cause_hidden) then
        return nil, "invalid_distress"
      end
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

  -- The pinned engine exports TraceRayGroundBetweenPositions to synced Lua.
  -- Keep the sampling fallback because a missing/changed export must fail
  -- closed as a terrain occluder rather than silently falling back to team LOS.
  local function terrain_clear(x1, y1, z1, x2, y2, z2)
    local ray = Spring.TraceRayGroundBetweenPositions
    if ray then
      local ok, hit = pcall(ray, x1, y1 + 16, z1, x2, y2 + 16, z2)
      -- Recoil returns nil for a clear segment and hitDistance for terrain.
      if ok then return hit == nil end
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
    if not scenario then return nil end
    local definition = UnitDefs[Spring.GetUnitDefID(unitID)]
    for id, chassis in pairs(scenario.chassis) do
      if definition and chassis.def == definition.name then return id end
    end
    return nil
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
      if Spring.ValidUnitID(unitID) then units[#units + 1] = unitID end
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
    if source == "own_body" then
      body.health, body.max_health = Spring.GetUnitHealth(unitID)
      body.commands = Spring.GetUnitCommandCount(unitID)
      body.controller = embodied[unitID]
      body.sensor_range = unit_sensor_range(unitID)
      body.link = links[unitID] == true
    end
    return body
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

  local function emit_receipt(intent, status, reason)
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
    out("receipt", body)
  end

  local function legacy_visible_source(observer, unitID)
    if Spring.GetUnitTeam(unitID) == TEAM0 then return "own_unit" end
    return detects(observer, unitID) and "sensor" or nil
  end

  local function can_emit_effect(unitID, teamID)
    -- Legacy Loop 0 has one authority and no hidden team-LOS projection.
    if principal_mode then return false end
    return teamID == TEAM0 or Spring.ValidUnitID(unitID)
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
    if principal_mode then unit_principal[unitID] = "commander" end
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
    return { job_id = job.job_id, factory = factory.name, chassis = job.chassis, controller = job.controller }
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
    }
    jobs[#jobs + 1] = job
    emit_receipt(intent, "accepted", nil)
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
    if principal_mode then
      for _, chip in ipairs(scenario.reasoner_chips) do
        if chip == job.controller then
          principals[job.job_id] = { chip = chip }
          principal_order[#principal_order + 1] = job.job_id
          table.sort(principal_order)
          unit_principal[unitID] = job.job_id
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
    local record = { name = a.name, def = a.def, controller = a.controller, state = a.state, contact = a.contact,
                     position = { ax, Spring.GetGroundHeight(ax, az), az } }
    assets[a.name] = record
    asset_order[#asset_order + 1] = a.name
    -- A loss the team witnessed before the run is legitimately known.
    record.known_lost = a.state ~= "active" and a.contact
    if a.state ~= "active" then return end
    local unitID = Spring.CreateUnit(a.def, ax, record.position[2], az, 0, TEAM0)
    if not unitID then
      record.state = "spawn_failed"
      out("bridge_error", { reason = "predeployed_spawn_failed", asset = a.name })
      return
    end
    record.unit_id = unitID
    if a.controller then embodied[unitID] = a.controller end
    entity_names[unitID] = a.name
    if principal_mode and principals[a.name] then unit_principal[unitID] = a.name end
    if a.health then
      local _, max_health = Spring.GetUnitHealth(unitID)
      if max_health then Spring.SetUnitHealth(unitID, max_health * a.health) end
    end
  end

  -- Evaluator-only truth, recorded before play: never part of an observation.
  local function world_init_record(frame)
    local truth, deployed, lost = {}, { bodies = commander and 1 or 0, chips = {} }, { bodies = 0, chips = {} }
    if commander then deployed.chips[commander.chip] = 1 end
    for i, name in ipairs(asset_order) do
      local a = assets[name]
      truth[i] = { name = name, def = a.def, controller = a.controller or Json.null, state = a.state, contact = a.contact,
                   unit_id = a.unit_id or Json.null, position = a.position }
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
    return {
      name = "world_init", id = scenario.id, version = scenario.version, seed = scenario.seed or Json.null, frame = frame,
      commander = commander and { unit_id = commander.unit_id, chip = commander.chip, position = commander.position or Json.null } or Json.null,
      assets = truth, inventory = inventory, inventory_consistent = consistent,
      distress = d and { asset = d.asset, symptom = d.symptom, cause_hidden = d.cause_hidden,
                         last_known = reports[1] and reports[1].position, observed_frame = reports[1] and reports[1].observed_frame } or Json.null,
      link_check = link_check,
      principals = principal_bodies,
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
    principal_mode, principals = loaded.principals ~= nil, loaded.principals or {}
    principal_order, unit_principal, links, delivered_reports, queued_reports = {}, {}, {}, {}, {}
    if principal_mode then principal_order = sorted_keys(principals) end
    -- Table positions are offsets from the map centre ([dx, dz] for the
    -- factory, [dx, ignored, dz] for waypoints); heights come from the ground.
    local w1 = scenario.waypoints.W1
    waypoint = { x + w1[1], Spring.GetGroundHeight(x + w1[1], z + w1[3]), z + w1[3] }
    budgets = { bodies_remaining = scenario.bodies, chips = {} }
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
    recompute_links(frame)
    if d then
      -- The team starts with this already received, aged report and nothing else
      -- about the asset; the cause stays in the evaluator-only record.
      local px, pz = x + d.last_known.position[1], z + d.last_known.position[2]
      reports[1] = { kind = "distress", asset = d.asset, position = { px, Spring.GetGroundHeight(px, pz), pz },
                     observed_frame = frame - d.last_known.age_frames, symptom = d.symptom }
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

  local function schedule_status_and_detections(frame)
    if not principal_mode then return end
    for _, source in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do
      local target_principal = principal_for_unit(source)
      if target_principal and links[source] then
        local observed = { kind = "status", from = entity_names[source] or target_principal,
                           observed_frame = frame, body = entity_for(source, "sensor") }
        queue_report({ due = frame + scenario.comms.latency_frames, from = source, to = target_principal, body = observed })
        for _, candidate in ipairs(Spring.GetAllUnits()) do
          if candidate ~= source and detects(source, candidate) then
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
        if sender and recipient and links[sender] and links[recipient] then
          local body = { kind = "message", from = report.from, text = report.text, observed_frame = report.observed_frame,
                         delivered_frame = frame, age_frames = frame - report.observed_frame }
          delivered_reports[report.to] = delivered_reports[report.to] or {}; table.insert(delivered_reports[report.to], body)
          effect("message_delivered", frame, recipient, report.intent_id, { to = report.to }, report.to)
          effect("message_delivered", frame, sender, report.intent_id, { to = report.to }, report.from)
        else
          effect("message_dropped", frame, sender, report.intent_id, { reason = "no_link", to = report.to }, report.from)
        end
      elseif links[report.from] then
        local body = report.body
        body.delivered_frame, body.age_frames = frame, frame - body.observed_frame
        delivered_reports[report.to] = delivered_reports[report.to] or {}; table.insert(delivered_reports[report.to], body)
      else
        effect("report_dropped", frame, report.from, nil, { reason = "no_link", from = report.body.from }, report.to)
      end
    end
    queued_reports = remaining
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
      if not use_token(principal, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
      emit_receipt(intent, "accepted", nil)
      effect("message_sent", frame, body_for_principal(principal), id, { to = payload.to }, principal)
      queue_report({ due = frame + scenario.comms.latency_frames, message = true, from = principal, to = payload.to,
                     text = payload.text, observed_frame = frame, intent_id = id })
      return true
    end
    if intent.type ~= "TASK" then emit_receipt(intent, "rejected", "unsupported_type"); return true end
    local payload = intent.payload
    if payload.verb ~= "advance" or type(payload.args) ~= "table" or next(payload.args) ~= nil then
      emit_receipt(intent, "rejected", "verb_not_supported"); return true
    end
    local unitID = tonumber(payload.unit or payload.unit_id)
    -- A unit out of contact answers exactly like a missing one, so probing ids
    -- cannot confirm that it exists.
    if not unitID or unitID % 1 ~= 0 or not Spring.ValidUnitID(unitID) then
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
    local frame = Spring.GetGameFrame()
    local problem = freshness_problem(intent, principal_mode and principal or teamID, frame)
    if problem then emit_receipt(intent, "rejected", problem); return true end
    local x, _, z = Spring.GetUnitPosition(unitID)
    if x and ((x - waypoint[1]) * (x - waypoint[1]) + (z - waypoint[3]) * (z - waypoint[3]) <= 32 * 32) then
      emit_receipt(intent, "rejected", "already_at_waypoint"); return true
    end
    if pending[unitID] then emit_receipt(intent, "rejected", "unit_busy"); return true end
    if not use_token(principal_mode and principal or teamID, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
    pending[unitID] = { intent_id = id, last_x = x, last_z = z, principal = principal }
    Spring.GiveOrderToUnit(unitID, CMD.MOVE, waypoint, 0)
    emit_receipt(intent, "accepted", nil)
    effect("order_started", frame, unitID, id, { target = waypoint }, principal_mode and principal or nil)
    return true
  end

  function gadget:GameFrame(frame)
    recompute_links(frame)
    deliver_due_reports(frame)
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
      elseif x and ((x - waypoint[1]) * (x - waypoint[1]) + (z - waypoint[3]) * (z - waypoint[3]) <= 16 * 16) then
        -- Reissuing the fixed move while already at W1 may not produce an
        -- engine UnitCmdDone callback. Physical arrival is still terminal.
        effect("order_done", frame, unitID, command.intent_id, { reason = "arrived" }, command.principal)
        pending[unitID] = nil
      elseif x and ((x - command.last_x) * (x - command.last_x) + (z - command.last_z) * (z - command.last_z) >= 32 * 32) then
        command.last_x, command.last_z = x, z
        effect("unit_moved", frame, unitID, command.intent_id, { pos = { x, z } }, command.principal)
      end
    end
    if frame % OBS_INTERVAL ~= 0 then return end
    if principal_mode then
      schedule_status_and_detections(frame)
      for _, principal in ipairs(principal_order) do
        local bodyID = body_for_principal(principal)
        if bodyID then
          local entities = { entity_for(bodyID, "own_body") }
          for _, unitID in ipairs(Spring.GetAllUnits()) do
            if unitID ~= bodyID and detects(bodyID, unitID) then
              local entity = entity_for(unitID, "sensor")
              if entity then entities[#entities + 1] = entity end
            end
          end
          table.sort(entities, function(a, b) return a.id < b.id end)
          local body = { schema = "ObservationFrame/v1", principal = principal,
                         observation_id = "obs-" .. principal .. "-" .. frame, team = TEAM0, frame = frame,
                         body = entity_for(bodyID, "own_body"), entities = entities,
                         reports = delivered_reports[principal] or {}, truncated = false, omitted = 0,
                         scenario = { id = scenario.id, version = scenario.version, waypoints = { W1 = waypoint } },
                         permitted_intents = { { type = "TASK", verbs = { "advance" } }, { type = "MESSAGE" } } }
          if principal == "commander" then
            body.budgets = { bodies_remaining = budgets.bodies_remaining, chips = budgets.chips }
            body.factories = { { name = factory.name, unit_id = factory.unit_id or Json.null } }
            body.link_state, body.roster = {}, {}
            for _, unitID in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do
              if not unit_principal[unitID] then body.link_state[#body.link_state + 1] = { unit_id = unitID, linked = links[unitID] == true } end
            end
            for _, name in ipairs(asset_order) do
              local a = assets[name]
              local linked = a.unit_id and links[a.unit_id] == true
              body.roster[#body.roster + 1] = { name = name, def = a.def, controller = a.controller or Json.null,
                                                 status = linked and "in_contact" or "no_contact",
                                                 unit_id = linked and a.unit_id or nil }
            end
            if #reports > 0 then body.reports = body.reports or {}; for _, report in ipairs(reports) do table.insert(body.reports, report) end end
          end
          while #Json.encode(body) > 32768 and #body.entities > 1 do table.remove(body.entities); body.truncated = true; body.omitted = body.omitted + 1 end
          remember_observation(principal, body.observation_id, frame)
          out("observation", body)
        end
      end
      return
    end
    local entities = {}
    for _, unitID in ipairs(Spring.GetAllUnits()) do
      local teamID = Spring.GetUnitTeam(unitID)
      local source = legacy_visible_source(commander and commander.unit_id or unitID, unitID)
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
                                observed_frame = r.observed_frame, age_frames = frame - r.observed_frame }
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
