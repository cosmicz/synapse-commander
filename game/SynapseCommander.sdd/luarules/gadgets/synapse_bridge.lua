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

  function gadget:GameStart()
    local frame = Spring.GetGameFrame()
    local x, z = Game.mapSizeX / 2, Game.mapSizeZ / 2
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
    local opts = Spring.GetModOptions() or {}
    out("hello", {
      protocol = "synapse-mailbox/0",
      run_id = opts.synapse_run_id,
      game = Game.gameName,
      map = Game.mapName,
      engine = Game.version,
      frame_rate = 30,
      teams = { TEAM0 },
      clock_mode = "continuous",
      scenario = { waypoints = { W1 = waypoint } },
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
    if has_raw_engine(intent, 1) then emit_receipt(intent, "rejected", "raw_engine_forbidden"); return true end
    local _, _, spectator, teamID = Spring.GetPlayerInfo(playerID, false)
    if spectator or tonumber(intent.team) ~= teamID then emit_receipt(intent, "rejected", "wrong_team"); return true end
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
    local frame = Spring.GetGameFrame()
    local observation_frame = observation_known(teamID, intent.based_on_observation_id)
    if not observation_frame then emit_receipt(intent, "rejected", "unknown_observation"); return true end
    if frame - observation_frame > 900 then emit_receipt(intent, "rejected", "stale_observation"); return true end
    if type(intent.expires_at_frame) ~= "number" or intent.expires_at_frame < frame then
      emit_receipt(intent, "rejected", "expired"); return true
    end
    if intent.expires_at_frame > frame + 900 then
      emit_receipt(intent, "rejected", "expiry_out_of_range"); return true
    end
    if pending[unitID] then emit_receipt(intent, "rejected", "unit_busy"); return true end
    if not use_token(teamID, frame) then emit_receipt(intent, "rejected", "rate_limited"); return true end
    local x, _, z = Spring.GetUnitPosition(unitID)
    Spring.GiveOrderToUnit(unitID, CMD.MOVE, waypoint, 0)
    pending[unitID] = { intent_id = id, last_x = x, last_z = z }
    emit_receipt(intent, "accepted", nil)
    out("effect", { schema = "EffectEvent/v1", effect = "order_started", frame = frame, unit_id = unitID, team = teamID, intent_id = id, detail = { target = waypoint } })
    return true
  end

  function gadget:GameFrame(frame)
    for unitID, command in pairs(pending) do
      local x, _, z = Spring.GetUnitPosition(unitID)
      if x and ((x - command.last_x) * (x - command.last_x) + (z - command.last_z) * (z - command.last_z) >= 32 * 32) then
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
          end
          entities[#entities + 1] = entity
        end
      end
    end
    table.sort(entities, function(a, b) return a.id < b.id end)
    while #entities > 32 do table.remove(entities) end
    local id = "obs-0-" .. frame
    local body = { schema = "ObservationFrame/v1", observation_id = id, team = TEAM0, frame = frame, entities = entities, scenario = { waypoints = { W1 = waypoint } }, permitted_intents = { { type = "TASK", verbs = { "advance" } } }, truncated = false, omitted = 0 }
    while #Json.encode(body) > 32768 and #entities > 0 do table.remove(entities); body.truncated = true; body.omitted = body.omitted + 1 end
    remember_observation(TEAM0, id, frame)
    out("observation", body)
  end

  function gadget:UnitCreated(unitID, _, teamID)
    if scenario_emitted and can_emit_effect(unitID, teamID) then out("effect", { schema = "EffectEvent/v1", effect = "unit_created", frame = Spring.GetGameFrame(), unit_id = unitID, team = teamID }) end
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
