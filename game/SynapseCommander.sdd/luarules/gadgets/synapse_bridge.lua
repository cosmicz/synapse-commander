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
    if type(t.commander) ~= "table" or not identifier(t.commander.chip) or t.chips[t.commander.chip] == nil then
      return nil, "invalid_commander"
    end
    if type(t.chassis) ~= "table" then return nil, "invalid_chassis" end
    local kinds = 0
    for id, c in pairs(t.chassis) do
      if not identifier(id) or type(c) ~= "table" or not identifier(c.def) or not UnitDefNames[c.def]
        or not count(c.build_frames) or not count(c.commission_frames) then
        return nil, "invalid_chassis"
      end
      kinds = kinds + 1
    end
    if kinds < 1 or kinds > 8 then return nil, "invalid_chassis" end
    local w1 = type(t.waypoints) == "table" and t.waypoints.W1
    if type(w1) ~= "table" or type(w1[1]) ~= "number" or type(w1[3]) ~= "number" then return nil, "invalid_waypoint" end
    return t
  end

  local function sorted_keys(map)
    local keys = {}
    for key in pairs(map) do keys[#keys + 1] = key end
    table.sort(keys)
    return keys
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

  local function visible_source(unitID, teamID, allyTeam)
    if teamID == TEAM0 then return "own_unit" end
    local state = Spring.GetUnitLosState(unitID, allyTeam, false)
    if state and state.los then return "los" end
    return nil
  end

  local function can_emit_effect(unitID, teamID)
    local _, _, _, _, _, allyTeam = Spring.GetTeamInfo(TEAM0, false)
    return visible_source(unitID, teamID, allyTeam) ~= nil
  end

  local function observation_known(teamID, id)
    local by_team = observations[teamID]
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

  local function remember_observation(teamID, id, frame)
    local by_team = observations[teamID] or {}
    local order = observation_order[teamID] or {}
    observations[teamID] = by_team
    observation_order[teamID] = order
    if not by_team[id] then order[#order + 1] = id end
    by_team[id] = frame
    while #order > MAX_OBSERVATIONS do
      by_team[table.remove(order, 1)] = nil
    end
  end

  local function effect(name, frame, unitID, intent_id, detail)
    out("effect", { schema = "EffectEvent/v1", effect = name, frame = frame, unit_id = unitID, team = TEAM0, intent_id = intent_id, detail = detail })
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
    embodied[unitID] = chip
    effect("commander_bound", Spring.GetGameFrame(), unitID, nil, { chip = chip })
  end

  -- Observation age and expiry, shared by TASK and PRODUCE in this order.
  local function freshness_problem(intent, teamID, frame)
    local observation_frame = observation_known(teamID, intent.based_on_observation_id)
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
    local problem = freshness_problem(intent, teamID, frame)
    if problem then emit_receipt(intent, "rejected", problem); return true end
    if #jobs >= MAX_JOBS then emit_receipt(intent, "rejected", "factory_queue_full"); return true end
    if budgets.bodies_remaining < 1 then emit_receipt(intent, "rejected", "budget_exhausted"); return true end
    if budgets.chips[payload.controller] < 1 then emit_receipt(intent, "rejected", "chip_unavailable"); return true end
    if not use_token(teamID, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
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
    effect("unit_created", frame, unitID, job.intent_id, { controller = job.controller, job_id = job.job_id, chassis = job.chassis })
    effect("factory_job_done", frame, factory.unit_id, job.intent_id, { job_id = job.job_id, unit_id = unitID })
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
    if not factory.unit_id then out("bridge_error", { reason = "factory_spawn_failed" }) end
    for _, unitID in ipairs(Spring.GetTeamUnits(TEAM0) or {}) do bind_commander(unitID) end
    return true
  end

  function gadget:GameStart()
    local frame = Spring.GetGameFrame()
    local x, z = Game.mapSizeX / 2, Game.mapSizeZ / 2
    local opts = Spring.GetModOptions() or {}
    local hello_scenario
    if opts.synapse_scenario and opts.synapse_scenario ~= "" then
      if not start_factory_scenario(opts.synapse_scenario, x, z) then return end
      hello_scenario = { id = scenario.id, version = scenario.version, waypoints = { W1 = waypoint } }
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
      frame = frame,
    })
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
    if scenario and intent.type == "PRODUCE" then return reduce_produce(intent, id, teamID) end
    if intent.type ~= "TASK" then emit_receipt(intent, "rejected", "unsupported_type"); return true end
    local payload = intent.payload
    if payload.verb ~= "advance" or type(payload.args) ~= "table" or next(payload.args) ~= nil then
      emit_receipt(intent, "rejected", "verb_not_supported"); return true
    end
    local unitID = tonumber(payload.unit or payload.unit_id)
    if not unitID or unitID % 1 ~= 0 or not Spring.ValidUnitID(unitID) then
      emit_receipt(intent, "rejected", "unknown_unit"); return true
    end
    if Spring.GetUnitTeam(unitID) ~= teamID then emit_receipt(intent, "rejected", "not_owned"); return true end
    -- The factory and any unbudgeted unit are not embodiments and take no orders.
    if scenario and not embodied[unitID] then emit_receipt(intent, "rejected", "not_embodied"); return true end
    local frame = Spring.GetGameFrame()
    local problem = freshness_problem(intent, teamID, frame)
    if problem then emit_receipt(intent, "rejected", problem); return true end
    local x, _, z = Spring.GetUnitPosition(unitID)
    if x and ((x - waypoint[1]) * (x - waypoint[1]) + (z - waypoint[3]) * (z - waypoint[3]) <= 32 * 32) then
      emit_receipt(intent, "rejected", "already_at_waypoint"); return true
    end
    if pending[unitID] then emit_receipt(intent, "rejected", "unit_busy"); return true end
    if not use_token(teamID, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
    pending[unitID] = { intent_id = id, last_x = x, last_z = z }
    Spring.GiveOrderToUnit(unitID, CMD.MOVE, waypoint, 0)
    emit_receipt(intent, "accepted", nil)
    out("effect", { schema = "EffectEvent/v1", effect = "order_started", frame = frame, unit_id = unitID, team = teamID, intent_id = id, detail = { target = waypoint } })
    return true
  end

  function gadget:GameFrame(frame)
    if scenario and #jobs > 0 then
      local remaining = {}
      for _, job in ipairs(jobs) do
        if not job.started and frame >= job.started_frame then
          job.started = true
          effect("factory_job_started", frame, factory.unit_id, job.intent_id, job_detail(job))
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
        out("effect", { schema = "EffectEvent/v1", effect = "order_done", frame = frame, unit_id = unitID, team = TEAM0, intent_id = command.intent_id, detail = { reason = "queue_empty" } })
        pending[unitID] = nil
      elseif x and ((x - waypoint[1]) * (x - waypoint[1]) + (z - waypoint[3]) * (z - waypoint[3]) <= 16 * 16) then
        -- Reissuing the fixed move while already at W1 may not produce an
        -- engine UnitCmdDone callback. Physical arrival is still terminal.
        out("effect", { schema = "EffectEvent/v1", effect = "order_done", frame = frame, unit_id = unitID, team = TEAM0, intent_id = command.intent_id, detail = { reason = "arrived" } })
        pending[unitID] = nil
      elseif x and ((x - command.last_x) * (x - command.last_x) + (z - command.last_z) * (z - command.last_z) >= 32 * 32) then
        command.last_x, command.last_z = x, z
        out("effect", { schema = "EffectEvent/v1", effect = "unit_moved", frame = frame, unit_id = unitID, team = TEAM0, intent_id = command.intent_id, detail = { pos = { x, z } } })
      end
    end
    if frame % OBS_INTERVAL ~= 0 then return end
    local _, _, _, _, _, allyTeam = Spring.GetTeamInfo(TEAM0, false)
    local entities = {}
    for _, unitID in ipairs(Spring.GetAllUnits()) do
      local teamID = Spring.GetUnitTeam(unitID)
      local source = visible_source(unitID, teamID, allyTeam)
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
      if can_emit_effect(unitID, teamID) then out("effect", { schema = "EffectEvent/v1", effect = "order_done", frame = Spring.GetGameFrame(), unit_id = unitID, team = teamID, intent_id = command.intent_id }) end
      pending[unitID] = nil
    end
  end

  function gadget:UnitDestroyed(unitID, _, teamID)
    local command = pending[unitID]
    if can_emit_effect(unitID, teamID) then out("effect", { schema = "EffectEvent/v1", effect = "unit_destroyed", frame = Spring.GetGameFrame(), unit_id = unitID, team = teamID, intent_id = command and command.intent_id }) end
    pending[unitID] = nil
    embodied[unitID] = nil
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
