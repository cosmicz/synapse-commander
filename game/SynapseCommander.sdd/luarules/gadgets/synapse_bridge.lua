function gadget:GetInfo() return {name="Synapse mailbox bridge", layer=0, enabled=true} end
local MAX=4096
if gadgetHandler:IsSyncedCode() then
  local Json=VFS.Include("common/luaUtilities/json.lua")
  local seen, observations, pending = {}, {}, {}
  local function out(kind, body) SendToUnsynced("synapse_out", kind, Json.encode(body), Spring.GetGameFrame()) end
  local function receipt(e,status,reason) out("receipt",{schema="IntentReceipt/v1",intent_id=e.intent_id,principal=e.principal,team=e.team,status=status,reason=reason,frame=Spring.GetGameFrame()}) end
  function gadget:GameStart()
    local x,z=Game.mapSizeX/2,Game.mapSizeZ/2; local unit=Spring.CreateUnit("armpw",x,Spring.GetGroundHeight(x,z),z,0,0); GG.synapse_unit=unit; GG.synapse_waypoint={x+400,Spring.GetGroundHeight(x+400,z),z}; out("hello",{protocol="synapse-mailbox/0",teams={0},clock_mode="continuous"})
  end
  function gadget:RecvLuaMsg(msg,player)
    if #msg>MAX or msg:sub(1,5)~="SYN0 " then return false end
    local ok,e=pcall(Json.decode,msg:sub(6)); if not ok or type(e)~="table" or type(e.envelope)~="table" then return true end; e=e.envelope
    local _,_,spec,team=Spring.GetPlayerInfo(player,false); if spec or e.team~=team then receipt(e,"rejected","wrong_team"); return true end
    if seen[e.intent_id] then receipt(e,"rejected","duplicate_intent_id"); return true end; seen[e.intent_id]=true
    if e.type~="TASK" then receipt(e,"rejected","unsupported_type"); return true end
    local p=e.payload or {}; if p.verb~="advance" or type(p.args)~="table" or next(p.args) then receipt(e,"rejected","verb_not_supported"); return true end
    if not observations[e.based_on_observation_id] then receipt(e,"rejected","unknown_observation"); return true end
    if Spring.GetUnitTeam(p.unit_id)~=team then receipt(e,"rejected","not_owned"); return true end
    local w=GG.synapse_waypoint; Spring.GiveOrderToUnit(p.unit_id,CMD.MOVE,w,0); pending[p.unit_id]=e.intent_id; receipt(e,"accepted",nil); out("effect",{schema="EffectEvent/v1",effect="order_started",frame=Spring.GetGameFrame(),unit_id=p.unit_id,team=team,intent_id=e.intent_id}) ; return true
  end
  function gadget:GameFrame(f) if f%15~=0 then return end; local u=GG.synapse_unit; local x,y,z=Spring.GetUnitPosition(u); local id="obs-0-"..f; observations[id]=f; out("observation",{schema="ObservationFrame/v1",observation_id=id,team=0,frame=f,entities={{id=u,pos={x,y,z},source="own_unit",observed_frame=f,age_frames=0}},scenario={waypoints={W1=GG.synapse_waypoint}},permitted_intents={{type="TASK",verbs={"advance"}}},truncated=false,omitted=0}) end
else
  local runID = Spring.GetConfigString("synapse_run_id", "")
  local path, outbox, offset, seq = "synapse/" .. runID .. "/inbox.jsonl", "synapse/" .. runID .. "/outbox.jsonl", 0, 0
  local function write(kind, body, frame) seq=seq+1; local f=io.open(outbox,"a"); if f then f:write(string.format('{"seq":%d,"frame":%d,"kind":"%s","body":%s}\n',seq,frame,kind,body)); f:close() end end
  local function bridge(_,kind,body,frame) write(kind,body,frame) end
  function gadget:Initialize() gadgetHandler:AddSyncAction("synapse_out",bridge) end
  function gadget:Shutdown() gadgetHandler:RemoveSyncAction("synapse_out") end
  function gadget:Update() local f=io.open(path,"r"); if not f then return end; f:seek("set",offset); local line=f:read("*l"); offset=f:seek(); f:close(); if line and #line<=MAX then Spring.SendLuaRulesMsg("SYN0 "..line) end end
end
