import datetime
import json
import os
import random
import socket
import socketserver
import time
import typing
import uuid
from itertools import chain

import pyxel

from source.display.board import Board
from source.foundation.catalogue import FACTION_COLOURS, LOBBY_NAMES, PLAYER_NAMES, Namer
from source.foundation.models import GameConfig, Player, PlayerDetails, LobbyDetails, Quad, Biome, ResourceCollection, \
    OngoingBlessing, InvestigationResult, Settlement
from source.game_management.game_controller import GameController
from source.game_management.game_state import GameState
from source.networking.client import dispatch_event
from source.networking.events import Event, EventType, CreateEvent, InitEvent, UpdateEvent, UpdateAction, \
    FoundSettlementEvent, QueryEvent, LeaveEvent, JoinEvent, RegisterEvent, SetBlessingEvent, SetConstructionEvent, \
    MoveUnitEvent, DeployUnitEvent, GarrisonUnitEvent, InvestigateEvent, BesiegeSettlementEvent
from source.saving.save_encoder import ObjectConverter, SaveEncoder
from source.saving.save_migrator import migrate_settlement, migrate_quad
from source.util.calculator import split_list_into_chunks

HOST, SERVER_PORT, CLIENT_PORT = "localhost", 9999, 55555


class RequestHandler(socketserver.BaseRequestHandler):
    def handle(self):
        evt: Event = json.loads(self.request[0], object_hook=ObjectConverter)
        sock: socket.socket = self.request[1]
        self.process_event(evt, sock)

    def process_event(self, evt: Event, sock: socket.socket):
        if evt.type == EventType.CREATE:
            self.process_create_event(evt, sock)
        elif evt.type == EventType.INIT:
            self.process_init_event(evt, sock)
        elif evt.type == EventType.UPDATE:
            self.process_update_event(evt, sock)
        elif evt.type == EventType.QUERY:
            self.process_query_event(evt, sock)
        elif evt.type == EventType.LEAVE:
            self.process_leave_event(evt)
        elif evt.type == EventType.JOIN:
            self.process_join_event(evt, sock)
        elif evt.type == EventType.REGISTER:
            self.process_register_event(evt)

    def process_create_event(self, evt: CreateEvent, sock: socket.socket):
        gsrs: typing.Dict[str, GameState] = self.server.game_states_ref
        if self.server.is_server:
            lobby_name = random.choice(LOBBY_NAMES)
            while lobby_name in gsrs:
                lobby_name = random.choice(LOBBY_NAMES)
            player_name = random.choice(PLAYER_NAMES)
            gsrs[lobby_name] = GameState()
            gsrs[lobby_name].players.append(Player(player_name, evt.cfg.player_faction,
                                                   FACTION_COLOURS[evt.cfg.player_faction]))
            self.server.game_clients_ref[lobby_name] = \
                [PlayerDetails(player_name, evt.cfg.player_faction, evt.identifier, self.client_address[0])]
            self.server.lobbies_ref[lobby_name] = evt.cfg
            resp_evt: CreateEvent = CreateEvent(evt.type, evt.timestamp, None, evt.cfg, lobby_name,
                                                self.server.game_clients_ref[lobby_name])
            sock.sendto(json.dumps(resp_evt, cls=SaveEncoder).encode(), self.server.clients_ref[evt.identifier])
        else:
            gsrs["local"].players.append(Player(evt.player_details[0].name, evt.cfg.player_faction,
                                                FACTION_COLOURS[evt.cfg.player_faction]))
            gc: GameController = self.server.game_controller_ref
            gc.menu.multiplayer_lobby_name = evt.lobby_name
            gc.menu.multiplayer_player_details = evt.player_details

    def process_init_event(self, evt: InitEvent, sock: socket.socket):
        gsrs: typing.Dict[str, GameState] = self.server.game_states_ref
        if self.server.is_server:
            gsr: GameState = gsrs[evt.game_name]
            gsr.game_started = True
            gsr.turn = 1
            random.seed()
            gsr.until_night = random.randint(10, 20)
            gsr.nighttime_left = 0
            gsr.on_menu = False
            # TODO This Namer is probably going to be a problem - other code will have to change to not use it for
            #  multiplayer games
            gsr.board = Board(self.server.lobbies_ref[evt.game_name], Namer())
            quads_list: typing.List[Quad] = list(chain.from_iterable(gsr.board.quads))
            for idx, quads_chunk in enumerate(split_list_into_chunks(quads_list, 100)):
                minified_quads: str = ""
                for quad in quads_chunk:
                    quad_str = f"{quad.biome.value[0]}{quad.wealth}{quad.harvest}{quad.zeal}{quad.fortune}"
                    if res := quad.resource:
                        if res.ore:
                            quad_str += "or"
                        elif res.timber:
                            quad_str += "t"
                        elif res.magma:
                            quad_str += "m"
                        elif res.aurora:
                            quad_str += "au"
                        elif res.bloodstone:
                            quad_str += "b"
                        elif res.obsidian:
                            quad_str += "ob"
                        elif res.sunstone:
                            quad_str += "s"
                        elif res.aquamarine:
                            quad_str += "aq"
                    if quad.is_relic:
                        quad_str += "ir"
                    quad_str += ","
                    minified_quads += quad_str
                for player in self.server.game_clients_ref[evt.game_name]:
                    resp_evt: InitEvent = InitEvent(evt.type, datetime.datetime.now(), None, evt.game_name,
                                                    gsr.until_night, self.server.lobbies_ref[evt.game_name],
                                                    minified_quads, idx)
                    sock.sendto(json.dumps(resp_evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[player.id])
        else:
            if not gsrs["local"].board:
                gsrs["local"].until_night = evt.until_night
                gsrs["local"].board = Board(evt.cfg, Namer(), [[None] * 100 for _ in range(90)],
                                            player_idx=gsrs["local"].player_idx, game_name=evt.game_name)
            split_quads: typing.List[str] = evt.quad_chunk.split(",")[:-1]
            for j in range(100):
                mini: str = split_quads[j]
                quad_biome: Biome
                match mini[0]:
                    case "D":
                        quad_biome = Biome.DESERT
                    case "F":
                        quad_biome = Biome.FOREST
                    case "S":
                        quad_biome = Biome.SEA
                    case "M":
                        quad_biome = Biome.MOUNTAIN
                quad_resource: typing.Optional[ResourceCollection] = None
                quad_is_relic: bool = False
                if len(mini) > 5:
                    if "or" in mini:
                        quad_resource = ResourceCollection(ore=1)
                    elif "t" in mini:
                        quad_resource = ResourceCollection(timber=1)
                    elif "m" in mini:
                        quad_resource = ResourceCollection(magma=1)
                    elif "au" in mini:
                        quad_resource = ResourceCollection(aurora=1)
                    elif "b" in mini:
                        quad_resource = ResourceCollection(bloodstone=1)
                    elif "ob" in mini:
                        quad_resource = ResourceCollection(obsidian=1)
                    elif "s" in mini:
                        quad_resource = ResourceCollection(sunstone=1)
                    elif "aq" in mini:
                        quad_resource = ResourceCollection(aquamarine=1)
                    quad_is_relic = mini.endswith("ir")
                expanded_quad: Quad = Quad(quad_biome, int(mini[1]), int(mini[2]), int(mini[3]), int(mini[4]),
                                           (j, evt.quad_chunk_idx), resource=quad_resource, is_relic=quad_is_relic)
                gsrs["local"].board.quads[evt.quad_chunk_idx][j] = expanded_quad
            # TODO busy wait bad fix later
            quads_populated: bool = True
            for i in range(90):
                for j in range(100):
                    if not gsrs["local"].board or gsrs["local"].board.quads[i][j] is None:
                        quads_populated = False
                        break
                if not quads_populated:
                    break
            if quads_populated:
                gc: GameController = self.server.game_controller_ref
                pyxel.mouse(visible=True)
                gc.last_turn_time = time.time()
                gsrs["local"].game_started = True
                gsrs["local"].on_menu = False
                # TODO add stats/achs later
                gsrs["local"].board.overlay.toggle_tutorial()
                gsrs["local"].board.overlay.total_settlement_count = \
                    sum(len(p.settlements) for p in gsrs["local"].players) + 1
                gc.music_player.stop_menu_music()
                gc.music_player.play_game_music()

    def process_update_event(self, evt: UpdateEvent, sock: socket.socket):
        match evt.action:
            case UpdateAction.FOUND_SETTLEMENT:
                self.process_found_settlement_event(evt, sock)
            case UpdateAction.SET_BLESSING:
                self.process_set_blessing_event(evt)
            case UpdateAction.SET_CONSTRUCTION:
                self.process_set_construction_event(evt)
            case UpdateAction.MOVE_UNIT:
                self.process_move_unit_event(evt, sock)
            case UpdateAction.DEPLOY_UNIT:
                self.process_deploy_unit_event(evt, sock)
            case UpdateAction.GARRISON_UNIT:
                self.process_garrison_unit_event(evt, sock)
            case UpdateAction.INVESTIGATE:
                self.process_investigate_event(evt, sock)
            case UpdateAction.BESIEGE_SETTLEMENT:
                self.process_besiege_settlement_event(evt, sock)

    def process_found_settlement_event(self, evt: FoundSettlementEvent, sock: socket.socket):
        gsrs: typing.Dict[str, GameState] = self.server.game_states_ref
        evt.settlement.location = (evt.settlement.location[0], evt.settlement.location[1])
        for i in range(len(evt.settlement.quads)):
            evt.settlement.quads[i] = migrate_quad(evt.settlement.quads[i],
                                                   (evt.settlement.location[0], evt.settlement.location[1]))
        if self.server.is_server:
            player = next(pl for pl in gsrs[evt.game_name].players if pl.faction == evt.player_faction)
            player.settlements.append(evt.settlement)
            for player in self.server.game_clients_ref[evt.game_name]:
                if player.faction != evt.player_faction:
                    sock.sendto(json.dumps(evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[player.id])
        else:
            player = next(pl for pl in gsrs["local"].players if pl.faction == evt.player_faction)
            player.settlements.append(evt.settlement)

    def process_set_blessing_event(self, evt: SetBlessingEvent):
        player = next(pl for pl in self.server.game_states_ref[evt.game_name].players
                      if pl.faction == evt.player_faction)
        player.ongoing_blessing = evt.blessing

    def process_set_construction_event(self, evt: SetConstructionEvent):
        player = next(pl for pl in self.server.game_states_ref[evt.game_name].players
                      if pl.faction == evt.player_faction)
        player.resources = evt.player_resources
        setl = next(setl for setl in player.settlements if setl.name == evt.settlement_name)
        setl.current_work = evt.construction

    def process_move_unit_event(self, evt: MoveUnitEvent, sock: socket.socket):
        game_name: str = evt.game_name if self.server.is_server else "local"
        player = next(pl for pl in self.server.game_states_ref[game_name].players
                      if pl.faction == evt.player_faction)
        unit = next(u for u in player.units if u.location == (evt.initial_loc[0], evt.initial_loc[1]))
        unit.location = (evt.new_loc[0], evt.new_loc[1])
        unit.remaining_stamina = evt.new_stamina
        unit.besieging = evt.besieging
        if self.server.is_server:
            for p in self.server.game_clients_ref[evt.game_name]:
                if p.faction != evt.player_faction:
                    sock.sendto(json.dumps(evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[p.id])

    def process_deploy_unit_event(self, evt: DeployUnitEvent, sock: socket.socket):
        game_name: str = evt.game_name if self.server.is_server else "local"
        player = next(pl for pl in self.server.game_states_ref[game_name].players
                      if pl.faction == evt.player_faction)
        setl = next(setl for setl in player.settlements if setl.name == evt.settlement_name)
        deployed = setl.garrison.pop()
        deployed.garrisoned = False
        deployed.location = (evt.location[0], evt.location[1])
        player.units.append(deployed)
        if self.server.is_server:
            for p in self.server.game_clients_ref[evt.game_name]:
                if p.faction != evt.player_faction:
                    sock.sendto(json.dumps(evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[p.id])

    def process_garrison_unit_event(self, evt: GarrisonUnitEvent, sock: socket.socket):
        game_name: str = evt.game_name if self.server.is_server else "local"
        player = next(pl for pl in self.server.game_states_ref[game_name].players
                      if pl.faction == evt.player_faction)
        unit = next(u for u in player.units if u.location == (evt.initial_loc[0], evt.initial_loc[1]))
        setl = next(setl for setl in player.settlements if setl.name == evt.settlement_name)
        unit.remaining_stamina = evt.new_stamina
        unit.garrisoned = True
        setl.garrison.append(unit)
        player.units.remove(unit)
        if self.server.is_server:
            for p in self.server.game_clients_ref[evt.game_name]:
                if p.faction != evt.player_faction:
                    sock.sendto(json.dumps(evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[p.id])

    def process_investigate_event(self, evt: InvestigateEvent, sock: socket.socket):
        game_name: str = evt.game_name if self.server.is_server else "local"
        player = next(pl for pl in self.server.game_states_ref[game_name].players
                      if pl.faction == evt.player_faction)
        unit = next(u for u in player.units if u.location == (evt.unit_loc[0], evt.unit_loc[1]))
        # TODO Loading from saves is eventually going to be a problem because we're ignoring quads_seen
        match evt.result:
            case InvestigationResult.FORTUNE:
                player.ongoing_blessing.fortune_consumed += player.ongoing_blessing.cost / 5
            case InvestigationResult.WEALTH:
                player.wealth += 25
            case InvestigationResult.HEALTH:
                unit.plan.max_health += 5
                unit.health += 5
            case InvestigationResult.POWER:
                unit.plan.power += 5
            case InvestigationResult.STAMINA:
                unit.plan.total_stamina += 1
                unit.remaining_stamina = unit.plan.total_stamina
            case InvestigationResult.UPKEEP:
                unit.plan.cost = 0
            case InvestigationResult.ORE:
                player.resources.ore += 10
            case InvestigationResult.TIMBER:
                player.resources.timber += 10
            case InvestigationResult.MAGMA:
                player.resources.magma += 10
        self.server.game_states_ref[game_name].board.quads[evt.relic_loc[1]][evt.relic_loc[0]].is_relic = False
        if self.server.is_server:
            for p in self.server.game_clients_ref[evt.game_name]:
                if p.faction != evt.player_faction:
                    sock.sendto(json.dumps(evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[p.id])

    def process_besiege_settlement_event(self, evt: BesiegeSettlementEvent, sock: socket.socket):
        game_name: str = evt.game_name if self.server.is_server else "local"
        player = next(pl for pl in self.server.game_states_ref[game_name].players
                      if pl.faction == evt.player_faction)
        unit = next(u for u in player.units if u.location == (evt.unit_loc[0], evt.unit_loc[1]))
        setl: Settlement
        for p in self.server.game_states_ref[game_name].players:
            for s in p.settlements:
                if s.name == evt.settlement_name:
                    setl = s
        unit.besieging = True
        setl.besieged = True
        if self.server.is_server:
            for p in self.server.game_clients_ref[evt.game_name]:
                if p.faction != evt.player_faction:
                    sock.sendto(json.dumps(evt, separators=(",", ":"), cls=SaveEncoder).encode(),
                                self.server.clients_ref[p.id])

    def process_query_event(self, evt: QueryEvent, sock: socket.socket):
        if self.server.is_server:
            lobbies: typing.List[LobbyDetails] = []
            for name, cfg in self.server.lobbies_ref.items():
                lobbies.append(LobbyDetails(name, self.server.game_clients_ref[name], cfg))
            resp_evt: QueryEvent = QueryEvent(EventType.QUERY, datetime.datetime.now(), None, lobbies)
            sock.sendto(json.dumps(resp_evt, cls=SaveEncoder).encode(), self.server.clients_ref[evt.identifier])
        else:
            gc: GameController = self.server.game_controller_ref
            gc.menu.multiplayer_lobbies = evt.lobbies

    def process_leave_event(self, evt: LeaveEvent):
        old_clients = self.server.game_clients_ref[evt.lobby_name]
        new_clients = [client for client in old_clients if client.id != evt.identifier]
        self.server.game_clients_ref[evt.lobby_name] = new_clients
        if not new_clients:
            self.server.game_clients_ref.pop(evt.lobby_name)
            self.server.lobbies_ref.pop(evt.lobby_name)
            self.server.game_states_ref.pop(evt.lobby_name)

    def process_join_event(self, evt: JoinEvent, sock: socket.socket):
        gsrs: typing.Dict[str, GameState] = self.server.game_states_ref
        if self.server.is_server:
            player_name = random.choice(PLAYER_NAMES)
            while any(player.name == player_name for player in self.server.game_clients_ref[evt.lobby_name]):
                player_name = random.choice(PLAYER_NAMES)
            gsrs[evt.lobby_name].players.append(Player(player_name, evt.player_faction,
                                                       FACTION_COLOURS[evt.player_faction]))
            self.server.game_clients_ref[evt.lobby_name].append(PlayerDetails(player_name, evt.player_faction,
                                                                              evt.identifier, self.client_address[0]))
            resp_evt: JoinEvent = JoinEvent(EventType.JOIN, datetime.datetime.now(), None, evt.lobby_name,
                                            evt.player_faction, self.server.game_clients_ref[evt.lobby_name])
            sock.sendto(json.dumps(resp_evt, cls=SaveEncoder).encode(), self.server.clients_ref[evt.identifier])
            for player in self.server.game_clients_ref[evt.lobby_name]:
                if player.id != evt.identifier:
                    update_evt: JoinEvent = JoinEvent(EventType.JOIN, datetime.datetime.now(), None, evt.lobby_name,
                                                      evt.player_faction, self.server.game_clients_ref[evt.lobby_name])
                    sock.sendto(json.dumps(update_evt, cls=SaveEncoder).encode(),
                                self.server.clients_ref[player.id])
        else:
            for idx, player in enumerate(evt.player_details):
                if player.faction == evt.player_faction:
                    gsrs["local"].player_idx = idx
                gsrs["local"].players.append(Player(player.name, player.faction, FACTION_COLOURS[player.faction]))
            gc: GameController = self.server.game_controller_ref
            gc.menu.multiplayer_lobby_name = evt.lobby_name
            gc.menu.multiplayer_player_details = evt.player_details

    def process_register_event(self, evt: RegisterEvent):
        self.server.clients_ref[evt.identifier] = self.client_address[0], evt.port


class EventListener:
    def __init__(self, is_server: bool = False):
        self.game_states: typing.Dict[str, GameState] = {}
        # self.events: typing.Dict[str, typing.List[Event]] = {}
        self.is_server: bool = is_server
        self.game_controller: typing.Optional[GameController] = None
        self.game_clients: typing.Dict[str, typing.List[PlayerDetails]] = {}
        self.lobbies: typing.Dict[str, GameConfig] = {}
        self.clients: typing.Dict[int, typing.Tuple[str, int]] = {}  # Hash identifier to (host, port).

    def run(self):
        with socketserver.UDPServer((HOST, SERVER_PORT if self.is_server else 0), RequestHandler) as server:
            if not self.is_server:
                dispatch_event(RegisterEvent(EventType.REGISTER, datetime.datetime.now(),
                                             hash((uuid.getnode(), os.getpid())), server.server_address[1]))
            server.game_states_ref = self.game_states
            # server.events_ref = self.events
            server.is_server = self.is_server
            server.game_controller_ref = self.game_controller
            server.game_clients_ref = self.game_clients
            server.lobbies_ref = self.lobbies
            server.clients_ref = self.clients
            server.serve_forever()


"""
Things to be initialised after the lobby is created:
- Turn
- Until night
- Nighttime left
- Players
- Board
- AIs (if any)
"""
