from struct import pack
from typing import NamedTuple
from utils.Logger import Logger

from database.realm.RealmDatabaseManager import RealmDatabaseManager, CharacterQuestState
from database.world.WorldDatabaseManager import WorldDatabaseManager
from game.world.managers.maps.MapManager import MapManager
from database.world.WorldModels import QuestTemplate
from game.world.managers.objects.item.ItemManager import ItemManager
from network.packet.PacketWriter import PacketWriter, OpCode
from utils.constants.ObjectCodes import QuestGiverStatus, QuestState, QuestFailedReasons, ObjectTypes
from utils.constants.UpdateFields import PlayerFields
from utils import Formulas

# Terminology:
# - quest or quest template refer to the quest template (the db record)
# - active_quest refers to quests in the player's quest log

MAX_QUEST_LOG = 20
QUEST_OBJECTIVES_COUNT = 4


class QuestManager(object):
    def __init__(self, player_mgr):
        self.player_mgr = player_mgr
        self.active_quests = {}
        self.completed_quests = set()

    def load_quests(self):
        quest_db_statuses = RealmDatabaseManager.character_get_quests(self.player_mgr.guid)

        for quest_db_status in quest_db_statuses:
            if quest_db_status.rewarded > 0:
                self.completed_quests.add(quest_db_status.quest)
            elif quest_db_status.state == QuestState.QUEST_ACCEPTED or quest_db_status.state == QuestState.QUEST_REWARD:
                self.active_quests[quest_db_status.quest] = ActiveQuest(quest_db_status)
            else:
                Logger.error(f"Quest database (guid={quest_db_status.guid}, quest_id={quest_db_status.quest}) has state {quest_db_status.state}. No handling.")

    def get_dialog_status(self, world_object):
        dialog_status = QuestGiverStatus.QUEST_GIVER_NONE
        # Relations bounds, the quest giver; Involved relations bounds, the quest completer
        relations_list = WorldDatabaseManager.QuestRelationHolder.creature_quest_get_by_entry(world_object.entry)
        involved_relations_list = WorldDatabaseManager.QuestRelationHolder.creature_involved_quest_get_by_entry(world_object.entry)
        if self.player_mgr.is_enemy_to(world_object):
            return dialog_status

        # Quest finish
        for involved_relation in involved_relations_list:
            if len(involved_relation) == 0:
                continue
            quest_entry = involved_relation[1]
            quest = WorldDatabaseManager.QuestTemplateHolder.quest_get_by_entry(quest_entry)
            if not quest:
                continue
            if quest_entry not in self.active_quests:
                continue
            quest_state = self.active_quests[quest_entry].state
            if quest_state == QuestState.QUEST_REWARD:
                return QuestState.QUEST_REWARD

        # Quest start
        for relation in relations_list:
            new_dialog_status = QuestGiverStatus.QUEST_GIVER_NONE
            quest_entry = relation[1]
            quest = WorldDatabaseManager.QuestTemplateHolder.quest_get_by_entry(quest_entry)
            if not quest or not self.check_quest_requirements(quest):
                continue

            if quest_entry in self.active_quests:
                continue
            if quest_entry in self.completed_quests:
                continue

            if quest.Method == 0:
                new_dialog_status = QuestGiverStatus.QUEST_GIVER_REWARD
            elif quest.MinLevel > self.player_mgr.level >= quest.MinLevel - 4:
                new_dialog_status = QuestGiverStatus.QUEST_GIVER_FUTURE
            elif quest.MinLevel <= self.player_mgr.level < quest.QuestLevel + 7:
                new_dialog_status = QuestGiverStatus.QUEST_GIVER_QUEST
            elif self.player_mgr.level > quest.QuestLevel + 7:
                new_dialog_status = QuestGiverStatus.QUEST_GIVER_TRIVIAL

            if new_dialog_status > dialog_status:
                dialog_status = new_dialog_status

        return dialog_status

    def prepare_quest_giver_gossip_menu(self, quest_giver, quest_giver_guid):
        quest_menu = QuestMenu()
        # Type is unit, but not player
        if quest_giver.get_type() == ObjectTypes.TYPE_UNIT and quest_giver.get_type() != ObjectTypes.TYPE_PLAYER:
            relations_list = WorldDatabaseManager.QuestRelationHolder.creature_quest_get_by_entry(quest_giver.entry)
            involved_relations_list = WorldDatabaseManager.QuestRelationHolder.creature_involved_quest_get_by_entry(quest_giver.entry)
        elif quest_giver.get_type() == ObjectTypes.TYPE_GAMEOBJECT:
            # TODO: Gameobjects
            relations_list = []
            involved_relations_list = []
        else:
            return

        # Quest finish
        for involved_relation in involved_relations_list:
            if len(involved_relation) == 0:
                continue
            quest_entry = involved_relation[1]
            quest = WorldDatabaseManager.QuestTemplateHolder.quest_get_by_entry(quest_entry)
            if not quest or not self.check_quest_requirements(quest) or not self.check_quest_level(quest, False):
                continue
            if quest_entry not in self.active_quests:
                continue
            quest_state = self.active_quests[quest_entry].state
            if quest_state < QuestState.QUEST_ACCEPTED:
                continue  # Quest accept is handled by relation_list
            quest_menu.add_menu_item(quest, quest_state)

        # Quest start
        for relation in relations_list:
            if len(relation) == 0:
                continue
            quest_entry = relation[1]
            quest = WorldDatabaseManager.QuestTemplateHolder.quest_get_by_entry(quest_entry)
            if not quest or not self.check_quest_requirements(quest) or not self.check_quest_level(quest, False):
                continue
            if quest_entry in self.completed_quests:
                continue
            quest_state = QuestState.QUEST_OFFER
            if quest_entry in self.active_quests:
                quest_state = self.active_quests[quest_entry].state
            if quest_state >= QuestState.QUEST_ACCEPTED:
                continue  # Quest turn-in is handled by involved_relations_list
            quest_menu.add_menu_item(quest, quest_state)

        if len(quest_menu.items) == 1:
            quest_menu_item = list(quest_menu.items.values())[0]
            if quest_menu_item.state == QuestState.QUEST_REWARD:
                self.send_quest_giver_offer_reward(self.active_quests[quest_menu_item.quest.entry], quest_giver_guid, True)
                return 0
            elif quest_menu_item.state == QuestState.QUEST_ACCEPTED:
                # TODO: Handle in progress quests
                return 0
            else:
                self.send_quest_giver_quest_details(quest_menu_item.quest, quest_giver_guid, True)
        else:
            # TODO: Send the proper greeting message
            self.send_quest_giver_quest_list("Greetings, $N.", quest_giver_guid, quest_menu.items)
        self.update_surrounding_quest_status()

    def check_quest_requirements(self, quest):
        # Is the player character the required race
        race_is_required = quest.RequiredRaces > 0
        if race_is_required and not (quest.RequiredRaces & self.player_mgr.race_mask):
            return False

        # Is the character the required class
        class_is_required = quest.RequiredClasses > 0
        if class_is_required and not (quest.RequiredClasses & self.player_mgr.class_mask):
            return False

        # Does the character have the required source item
        source_item_required = quest.SrcItemId > 0
        does_not_have_source_item = self.player_mgr.inventory.get_item_count(quest.SrcItemId) == 0
        if source_item_required and does_not_have_source_item:
            return False

        # Has the character already started the next quest in the chain
        if quest.NextQuestInChain > 0 and quest.NextQuestInChain in self.completed_quests:
            return False

        # Does the character have the previous quest
        if quest.PrevQuestId > 0 and quest.PrevQuestId not in self.completed_quests:
            return False

        # TODO: Does the character have the required skill
        
        return True

    def check_quest_level(self, quest, will_send_response):
        if self.player_mgr.level < quest.MinLevel:
            if will_send_response:
                self.send_cant_take_quest_response(QuestFailedReasons.INVALIDREASON_QUEST_FAILED_LOW_LEVEL)
            return False
        else:
            return True
    
    @staticmethod
    def check_quest_giver_npc_is_related(quest_giver_entry, quest_entry):
        is_related = False
        relations_list = WorldDatabaseManager.QuestRelationHolder.creature_quest_get_by_entry(quest_giver_entry)
        for relation in relations_list:
            if relation.entry == quest_giver_entry and relation.quest == quest_entry:
                is_related = True
        return is_related

    @staticmethod
    def generate_rew_choice_item_list(quest):
        return [quest.RewChoiceItemId1, quest.RewChoiceItemId2, quest.RewChoiceItemId3, quest.RewChoiceItemId4,
                quest.RewChoiceItemId5, quest.RewChoiceItemId6]

    @staticmethod
    def generate_rew_choice_count_list(quest):
        return [quest.RewChoiceItemCount1, quest.RewChoiceItemCount2, quest.RewChoiceItemCount3,
                quest.RewChoiceItemCount4, quest.RewChoiceItemCount5, quest.RewChoiceItemCount6]

    @staticmethod
    def generate_rew_item_list(quest):
        return [quest.RewItemId1, quest.RewItemId3, quest.RewItemId2, quest.RewItemId4]

    @staticmethod
    def generate_rew_count_list(quest):
        return [quest.RewItemCount1, quest.RewItemCount2, quest.RewItemCount3, quest.RewItemCount4]

    @staticmethod
    def generate_req_item_list(quest):
        return [quest.ReqItemId1, quest.ReqItemId2, quest.ReqItemId3, quest.ReqItemId4]

    @staticmethod
    def generate_req_item_count_list(quest):
        return [quest.ReqItemCount1, quest.ReqItemCount2, quest.ReqItemCount3, quest.ReqItemCount4]

    @staticmethod
    def generate_req_source_list(quest):
        return [quest.ReqSourceId1, quest.ReqSourceId2, quest.ReqSourceId3, quest.ReqSourceId4]

    @staticmethod
    def generate_req_source_count_list(quest):
        return [quest.ReqSourceCount1, quest.ReqSourceCount2, quest.ReqSourceCount3, quest.ReqSourceCount4]

    @staticmethod
    def generate_req_creature_or_go_list(quest):
        return [quest.ReqCreatureOrGOId1, quest.ReqCreatureOrGOId2, quest.ReqCreatureOrGOId3, quest.ReqCreatureOrGOId4]

    @staticmethod
    def generate_req_creature_or_go_count_list(quest):
        return [quest.ReqCreatureOrGOCount1, quest.ReqCreatureOrGOCount2, quest.ReqCreatureOrGOCount3, quest.ReqCreatureOrGOCount4]

    @staticmethod
    def generate_req_spell_cast_list(quest):
        return [quest.ReqSpellCast1, quest.ReqSpellCast2, quest.ReqSpellCast3, quest.ReqSpellCast4]

    @staticmethod
    def generate_objective_text_list(quest):
        return [quest.ObjectiveText1, quest.ObjectiveText2, quest.ObjectiveText3, quest.ObjectiveText4]

    def update_surrounding_quest_status(self):
        for guid, unit in list(MapManager.get_surrounding_units(self.player_mgr).items()):
            if WorldDatabaseManager.QuestRelationHolder.creature_involved_quest_get_by_entry(unit.entry) or WorldDatabaseManager.QuestRelationHolder.creature_quest_get_by_entry(unit.entry):
                quest_status = self.get_dialog_status(unit)
                self.send_quest_giver_status(guid, quest_status)

    # Send item query details and return item struct byte segments.
    def _gen_item_struct(self, item_entry, count, include_display_id=True):
        item_template = WorldDatabaseManager.ItemTemplateHolder.item_template_get_by_entry(item_entry)
        display_id = 0
        if item_template:
            item_mgr = ItemManager(item_template=item_template)
            self.player_mgr.session.enqueue_packet(item_mgr.query_details())
            display_id = item_template.display_id

        item_data = pack(
            '<2I',
            item_entry,
            count
        )
        if include_display_id:
            item_data += pack('<I', display_id)

        return item_data

    def send_cant_take_quest_response(self, reason_code):
        data = pack('<I', reason_code)
        self.player_mgr.session.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_QUESTGIVER_QUEST_INVALID, data))

    def send_quest_giver_status(self, quest_giver_guid, quest_status):
        data = pack(
            '<QI',
            quest_giver_guid if quest_giver_guid > 0 else self.player_mgr.guid,
            quest_status
        )
        self.player_mgr.session.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_QUESTGIVER_STATUS, data))

    def send_quest_giver_quest_list(self, message, quest_giver_guid, quests):
        message_bytes = PacketWriter.string_to_bytes(message)
        data = pack(
            f'<Q{len(message_bytes)}s2iB',
            quest_giver_guid,
            message_bytes,
            0,  # TODO: delay
            0,  # TODO: emoteID
            len(quests)
        )

        for entry in quests:
            quest_title = PacketWriter.string_to_bytes(quests[entry].quest.Title)
            data += pack(
                f'<3I{len(quest_title)}s',
                entry,
                quests[entry].state,
                quests[entry].quest.QuestLevel,
                quest_title
            )

        self.player_mgr.session.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_QUESTGIVER_QUEST_LIST, data))

    def send_quest_giver_quest_details(self, quest, quest_giver_guid, activate_accept):
        # Quest information
        quest_title = PacketWriter.string_to_bytes(quest.Title)
        quest_details = PacketWriter.string_to_bytes(quest.Details)
        quest_objectives = PacketWriter.string_to_bytes(quest.Objectives)
        data = pack(
            f'<QI{len(quest_title)}s{len(quest_details)}s{len(quest_objectives)}sI',
            quest_giver_guid,
            quest.entry,
            quest_title,
            quest_details,
            quest_objectives,
            1 if activate_accept else 0
        )

        # Reward choices
        rew_choice_item_list = list(filter((0).__ne__, self.generate_rew_choice_item_list(quest)))
        rew_choice_count_list = list(filter((0).__ne__, self.generate_rew_choice_count_list(quest)))
        data += pack('<I', len(rew_choice_item_list))
        for index, item in enumerate(rew_choice_item_list):
            data += self._gen_item_struct(item, rew_choice_count_list[index])

        # Reward items
        rew_item_list = list(filter((0).__ne__, self.generate_rew_item_list(quest)))
        rew_count_list = list(filter((0).__ne__, self.generate_rew_count_list(quest)))
        data += pack('<I', len(rew_item_list))
        for index, item in enumerate(rew_item_list):
            data += self._gen_item_struct(item, rew_count_list[index])

        # Reward money
        data += pack('<I', quest.RewOrReqMoney)

        # Required items
        req_item_list = list(filter((0).__ne__, self.generate_req_item_list(quest)))
        req_count_list = list(filter((0).__ne__, self.generate_req_item_count_list(quest)))
        data += pack('<I', len(req_item_list))
        for index, item in enumerate(req_item_list):
            data += self._gen_item_struct(item, req_count_list[index], include_display_id=False)

        # Required kill / item count
        req_creature_or_go_list = list(filter((0).__ne__, self.generate_req_creature_or_go_list(quest)))
        req_creature_or_go_count_list = list(filter((0).__ne__, self.generate_req_creature_or_go_count_list(quest)))
        data += pack('<I', len(req_creature_or_go_list))
        for index, creature_or_go in enumerate(req_creature_or_go_list):
            data += pack(
                '<2I',
                creature_or_go if creature_or_go >= 0 else (creature_or_go * -1) | 0x80000000,
                req_creature_or_go_count_list[index]
            )

        self.player_mgr.session.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_QUESTGIVER_QUEST_DETAILS, data))

    def send_quest_query_response(self, active_quest):
        quest = active_quest.quest
        data = pack(
            f'<3Ii4I',
            quest.entry,
            quest.Method,
            quest.QuestLevel,
            quest.ZoneOrSort,
            quest.Type,
            quest.NextQuestInChain,
            quest.RewOrReqMoney,
            quest.SrcItemId
        )

        # Rew items
        rew_item_list = self.generate_rew_item_list(quest)
        rew_item_count_list = self.generate_rew_count_list(quest)
        for index, item in enumerate(rew_item_list):
            data += pack('<2I', item, rew_item_count_list[index])

        # Reward choices
        rew_choice_item_list = self.generate_rew_choice_item_list(quest)
        rew_choice_count_list = self.generate_rew_choice_count_list(quest)
        for index, item in enumerate(rew_choice_item_list):
            data += pack('<2I', item, rew_choice_count_list[index])

        title_bytes = PacketWriter.string_to_bytes(quest.Title)
        details_bytes = PacketWriter.string_to_bytes(quest.Details)
        objectives_bytes = PacketWriter.string_to_bytes(quest.Objectives)
        end_bytes = PacketWriter.string_to_bytes(quest.EndText)
        data += pack(
            f'<I2fI{len(title_bytes)}s{len(details_bytes)}s{len(objectives_bytes)}s{len(end_bytes)}s',
            quest.PointMapId,
            quest.PointX,
            quest.PointY,
            quest.PointOpt,
            title_bytes,
            details_bytes,
            objectives_bytes,
            end_bytes,
        )

        # Required kills / Required items count
        req_creature_or_go_list = self.generate_req_creature_or_go_list(quest)
        req_creature_or_go_count_list = self.generate_req_creature_or_go_count_list(quest)
        req_item_list = self.generate_req_item_list(quest)
        req_count_list = self.generate_req_item_count_list(quest)
        for index, creature_or_go in enumerate(req_creature_or_go_list):
            data += pack(
                '<4I',
                creature_or_go if creature_or_go >= 0 else (creature_or_go * -1) | 0x80000000,
                req_creature_or_go_count_list[index],
                req_item_list[index],
                req_count_list[index]
            )

        # Objective texts
        req_objective_text_list = self.generate_objective_text_list(quest)
        for index, objective_text in enumerate(req_objective_text_list):
            req_objective_text_bytes = PacketWriter.string_to_bytes(req_objective_text_list[index])
            data += pack(
                f'{len(req_objective_text_bytes)}s',
                req_objective_text_bytes
            )

        self.player_mgr.session.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_QUEST_QUERY_RESPONSE, data))

    def send_quest_giver_offer_reward(self, active_quest, quest_giver_guid, enable_next=True):
        # CGPlayer_C::OnQuestGiverChooseReward
        quest = active_quest.quest
        quest_title = PacketWriter.string_to_bytes(quest.Title)
        quest_offer_reward_text = PacketWriter.string_to_bytes(quest.OfferRewardText)
        data = pack(
            f'<QI{len(quest_title)}s{len(quest_offer_reward_text)}sI',
            quest_giver_guid,
            quest.entry,
            quest_title,
            quest_offer_reward_text,
            1 if enable_next else 0  # enable_next
        )

        # TODO Handle emotes
        # Emote count
        data += pack('<I', 0)
        # for i in range(4):
        #     data += pack('<2I', 0, 0)

        # Reward choices
        rew_choice_item_list = list(filter((0).__ne__, self.generate_rew_choice_item_list(quest)))
        rew_choice_count_list = list(filter((0).__ne__, self.generate_rew_choice_count_list(quest)))
        data += pack('<I', len(rew_choice_item_list))
        for index, item in enumerate(rew_choice_item_list):
            data += self._gen_item_struct(item, rew_choice_count_list[index])

        # Required items
        req_item_list = list(filter((0).__ne__, self.generate_req_item_list(quest)))
        req_count_list = list(filter((0).__ne__, self.generate_req_item_count_list(quest)))
        data += pack('<I', len(req_item_list))
        for index, item in enumerate(req_item_list):
            data += self._gen_item_struct(item, req_count_list[index], include_display_id=False)

        # Reward
        data += pack('<I', quest.RewOrReqMoney if quest.RewOrReqMoney >= 0 else -quest.RewOrReqMoney)
        data += pack('<I', quest.RewSpell)
        data += pack('<I', quest.RewSpellCast)

        self.player_mgr.session.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_QUESTGIVER_OFFER_REWARD, data))

    def handle_add_quest(self, quest_id, quest_giver_guid):
        active_quest = ActiveQuest(self.create_db_quest_status(quest_id))
        self.active_quests[quest_id] = active_quest
        self.send_quest_query_response(active_quest)

        if self.can_complete_quest(active_quest):
            self.complete_quest(active_quest)

        self.update_surrounding_quest_status()

        self.build_update()
        self.player_mgr.send_update_self()

        RealmDatabaseManager.character_add_quest_status(active_quest.quest_db_status)

    def handle_remove_quest(self, slot):
        quest_id = self.player_mgr.get_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6))
        if quest_id in self.active_quests:
            self.remove_from_questlog(quest_id)
            RealmDatabaseManager.character_delete_quest(self.player_mgr.guid, quest_id)

    def create_db_quest_status(self, quest_id):
        db_quest_status = CharacterQuestState()
        db_quest_status.guid = self.player_mgr.guid
        db_quest_status.quest = quest_id
        db_quest_status.state = QuestState.QUEST_ACCEPTED.value
        return db_quest_status

    def handle_complete_quest(self, quest_id, quest_giver_guid):
        if quest_id not in self.active_quests:
            return
        active_quest = self.active_quests[quest_id]
        if not self.is_quest_complete(active_quest, quest_giver_guid):
            return
        self.send_quest_giver_offer_reward(active_quest, quest_giver_guid, True)

    def handle_choose_reward(self, quest_giver_guid, quest_id, item_choice):
        if quest_id not in self.active_quests:
            return
        active_quest = self.active_quests[quest_id]
        if not self.is_quest_complete(active_quest, quest_giver_guid):
            return

        self.reward_xp(active_quest)
        self.reward_gold(active_quest)
        # self.reward_item(active_quest, item_choice)

        # Remove from log and mark as rewarded
        self.remove_from_questlog(quest_id)
        self.completed_quests.add(quest_id)
        active_quest.quest_db_status.rewarded = 1
        RealmDatabaseManager.character_update_quest_status(active_quest.quest_db_status)

        if active_quest.quest.NextQuestInChain > 0:
            next_quest_id = active_quest.quest.NextQuestInChain
            if next_quest_id not in self.active_quests and next_quest_id not in self.completed_quests:
                next_quest = WorldDatabaseManager.QuestTemplateHolder.quest_get_by_entry(next_quest_id)
                self.send_quest_giver_quest_details(next_quest, quest_giver_guid, True)

        # TODO: If no next quest, how to get the dialog to close? (maybe CGUnit_C::NPCFlagChanged)

    def remove_from_questlog(self, quest_id):
        del self.active_quests[quest_id]
        self.update_surrounding_quest_status()
        self.set_questlog_entry(len(self.active_quests), 0)
        self.build_update()
        self.player_mgr.send_update_self()

    def reward_xp(self, active_quest):
        self.player_mgr.give_xp([Formulas.PlayerFormulas.quest_xp_reward(active_quest.quest.QuestLevel,
                                                                         self.player_mgr.level,
                                                                         active_quest.quest.RewXP)])

    def reward_gold(self, active_quest):
        if active_quest.quest.RewOrReqMoney != 0:
            self.player_mgr.mod_money(active_quest.quest.RewOrReqMoney)

    def build_update(self):
        for slot, quest_id in enumerate(self.active_quests.keys()):
            self.set_questlog_entry(slot, quest_id)

    def set_questlog_entry(self, slot, quest_id):
        self.player_mgr.set_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6), quest_id)
        # TODO Finish / investigate below values
        self.player_mgr.set_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6) + 1, 0)  # quest giver ID ?
        self.player_mgr.set_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6) + 2, 0)  # quest rewarder ID ?
        self.player_mgr.set_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6) + 3, 0)  # quest progress
        self.player_mgr.set_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6) + 4, 0)  # quest failure time
        self.player_mgr.set_uint32(PlayerFields.PLAYER_QUEST_LOG_1_1 + (slot * 6) + 5, 0)  # number of mobs to kill

    def is_instant_complete_quest(self, quest):
        req_item_list = self.generate_req_item_list(quest)
        for index, req_item in enumerate(req_item_list):
            if req_item > 0:
                return False

        req_source_list = self.generate_req_source_list(quest)
        for index, req_source in enumerate(req_source_list):
            if req_source > 0:
                return False

        req_creature_or_go_count_list = self.generate_req_creature_or_go_count_list(quest)
        for index, creature_or_go in enumerate(req_creature_or_go_count_list):
            if creature_or_go > 0:
                return False

        req_spell_cast_list = self.generate_req_spell_cast_list(quest)
        for index, req_spell_cast in enumerate(req_spell_cast_list):
            if req_spell_cast > 0:
                return False

        return True

    def can_complete_quest(self, active_quest):
        return self.is_instant_complete_quest(active_quest.quest)

    def complete_quest(self, active_quest):
        active_quest.state = QuestState.QUEST_REWARD

    def is_quest_complete(self, active_quest, quest_giver_guid):
        if active_quest.state != QuestState.QUEST_REWARD:
            return False
        # TODO: check that quest_giver_guid is turn-in for quest_id
        return True

    def is_quest_item_required(self, item_entry):
        for active_quest in list(self.active_quests.values()):
            if item_entry in QuestManager.generate_req_item_list(active_quest.quest):
                return True
        return False


class QuestMenu:
    class QuestMenuItem(NamedTuple):
        quest: QuestTemplate
        state: QuestState

    def __init__(self):
        self.items = {}

    def add_menu_item(self, quest, state):
        self.items[quest.entry] = QuestMenu.QuestMenuItem(quest, state)

    def clear_menu(self):
        self.items.clear()


class ActiveQuest:
    def __init__(self, quest_db_status):
        self.quest_db_status = quest_db_status
        self.quest_id = quest_db_status.quest
        self.state = QuestState(quest_db_status.state)
        self.quest = WorldDatabaseManager.QuestTemplateHolder.quest_get_by_entry(self.quest_id)
