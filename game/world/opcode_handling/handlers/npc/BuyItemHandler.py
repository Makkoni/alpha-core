from struct import unpack

from database.world.WorldDatabaseManager import WorldDatabaseManager
from game.world.managers.maps.MapManager import MapManager
from utils.constants.ObjectCodes import BuyResults


class BuyItemHandler(object):

    @staticmethod
    def handle(world_session, socket, reader):
        if len(reader.data) >= 13:  # Avoid handling empty buy item packet.
            vendor_guid, item, count = unpack('<QIB', reader.data[:13])

            if vendor_guid > 0:
                if count <= 0:
                    count = 1

                vendor_npc = MapManager.get_surrounding_unit_by_guid(world_session.player_mgr, vendor_guid)
                vendor_data, session = WorldDatabaseManager.creature_get_vendor_data_by_item(vendor_npc.entry, item)

                if vendor_data:
                    item_template = vendor_data.item_template
                    session.close()

                    total_cost = item_template.buy_price * count

                    if world_session.player_mgr.coinage < total_cost:
                        world_session.player_mgr.inventory.send_buy_error(BuyResults.BUY_ERR_NOT_ENOUGH_MONEY, item,
                                                                          vendor_guid)
                        return 0

                    if 0 < vendor_data.maxcount < count:  # I should be checking here for current count too
                        world_session.player_mgr.inventory.send_buy_error(BuyResults.BUY_ERR_ITEM_SOLD_OUT, item,
                                                                          vendor_guid)
                        return 0

                    if world_session.player_mgr.inventory.add_item(item_template=item_template, count=count,
                                                                   handle_error=False):
                        world_session.player_mgr.mod_money(total_cost * -1)
                        #vendor_npc.send_inventory_list(world_session)
                    else:
                        world_session.player_mgr.inventory.send_buy_error(BuyResults.BUY_ERR_CANT_CARRY_MORE, item,
                                                                          vendor_guid)
                else:
                    world_session.player_mgr.inventory.send_buy_error(BuyResults.BUY_ERR_CANT_FIND_ITEM, item,
                                                                      vendor_guid)

        return 0
