from struct import unpack
from utils.constants.UpdateFields import *
from utils.constants.UnitCodes import UnitFlags
from game.world.managers.maps.MapManager import MapManager


class LootRequestHandler(object):

    @staticmethod
    def handle(world_session, socket, reader):
        if len(reader.data) >= 8:  # Avoid handling empty loot packet.
            loot_target_guid = unpack('<Q', reader.data[:8])[0]

            player = world_session.player_mgr
            enemy = MapManager.get_surrounding_unit_by_guid(world_session.player_mgr, loot_target_guid,
                                                            include_players=False)

            if player and enemy:
                # Only set flag if player was able to loot, else the player would be kneeling forever.
                if player.send_loot(enemy):
                    player.unit_flags |= UnitFlags.UNIT_FLAG_LOOTING
                    player.set_uint32(UnitFields.UNIT_FIELD_FLAGS, player.unit_flags)
                    player.set_dirty()

        return 0
