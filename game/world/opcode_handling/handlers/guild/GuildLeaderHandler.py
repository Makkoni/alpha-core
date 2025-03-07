from network.packet.PacketReader import *
from game.world.managers.objects.player.guild.GuildManager import GuildManager
from utils.constants.ObjectCodes import GuildCommandResults, GuildTypeCommand, GuildRank
from database.realm.RealmDatabaseManager import RealmDatabaseManager


class GuildLeaderHandler(object):

    @staticmethod
    def handle(world_session, socket, reader):
        if len(reader.data) > 1:  # Avoid handling empty Guild Demote packet.
            target_name = PacketReader.read_string(reader.data, 0).strip()
            target_player_mgr = RealmDatabaseManager.character_get_by_name(target_name)
            player_mgr = world_session.player_mgr

            if not player_mgr.guild_manager:
                GuildManager.send_guild_command_result(player_mgr, GuildTypeCommand.GUILD_INVITE_S, '',
                                                       GuildCommandResults.GUILD_PLAYER_NOT_IN_GUILD)
            elif not target_player_mgr:
                GuildManager.send_guild_command_result(player_mgr, GuildTypeCommand.GUILD_INVITE_S, target_name,
                                                       GuildCommandResults.GUILD_PLAYER_NOT_FOUND)
            elif not player_mgr.guild_manager.has_member(target_player_mgr.guid):
                GuildManager.send_guild_command_result(player_mgr, GuildTypeCommand.GUILD_INVITE_S, target_name,
                                                       GuildCommandResults.GUILD_PLAYER_NOT_IN_GUILD)
            elif player_mgr.guild_manager.get_rank(player_mgr.guid) != GuildRank.GUILDRANK_GUILD_MASTER:
                GuildManager.send_guild_command_result(player_mgr, GuildTypeCommand.GUILD_FOUNDER_S, '',
                                                       GuildCommandResults.GUILD_PERMISSIONS)
            elif player_mgr.guid == target_player_mgr.guid:
                GuildManager.send_guild_command_result(player_mgr, GuildTypeCommand.GUILD_FOUNDER_S, '',
                                                       GuildCommandResults.GUILD_INTERNAL)
            else:
                player_mgr.guild_manager.set_guild_master(target_player_mgr.guid)

        return 0
