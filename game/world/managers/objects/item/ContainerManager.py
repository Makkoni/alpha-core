from database.realm.RealmDatabaseManager import RealmDatabaseManager
from game.world.WorldSessionStateHandler import WorldSessionStateHandler
from game.world.managers.objects.item.ItemManager import ItemManager
from utils.constants.ItemCodes import InventorySlots, ItemClasses, ItemSubClasses, BagFamilies
from utils.constants.ObjectCodes import ObjectTypes, ObjectTypeIds, HighGuid, ItemBondingTypes
from utils.constants.UpdateFields import ContainerFields

MAX_BAG_SLOTS = 20  # (ContainerFields.CONTAINER_END - ContainerFields.CONTAINER_FIELD_SLOT_1) / 2


class ContainerManager(ItemManager):
    def __init__(self, owner, item_template=None, item_instance=None, is_backpack=False, **kwargs):
        super().__init__(item_template, item_instance, **kwargs)

        self.guid = (item_instance.guid if item_instance else 0) | HighGuid.HIGHGUID_CONTAINER
        self.owner = owner
        self.is_backpack = is_backpack
        if self.is_backpack:
            self.current_slot = InventorySlots.SLOT_INBACKPACK.value

        self.sorted_slots = dict()

        if not self.is_backpack:
            self.total_slots = self.item_template.container_slots
            self.start_slot = 0
            self.max_slot = self.total_slots
            self.is_contained = self.guid
        else:
            self.total_slots = InventorySlots.SLOT_ITEM_END - InventorySlots.SLOT_ITEM_START
            self.start_slot = InventorySlots.SLOT_ITEM_START
            self.max_slot = InventorySlots.SLOT_BANK_END
            self.is_contained = self.owner

        self.object_type.append(ObjectTypes.TYPE_CONTAINER)
        self.update_packet_factory.init_values(ContainerFields.CONTAINER_END)

    @classmethod
    def from_item(cls, item_manager):
        return cls(
            owner=item_manager.item_instance.owner.guid,
            item_template=item_manager.item_template,
            item_instance=item_manager.item_instance
        )

    def build_container_update_packet(self):
        self.set_uint32(ContainerFields.CONTAINER_FIELD_NUM_SLOTS, self.item_template.container_slots)

        for x in range(0, MAX_BAG_SLOTS):
            guid = self.sorted_slots[x].guid if x in self.sorted_slots else 0
            self.set_uint64(ContainerFields.CONTAINER_FIELD_SLOT_1 + x * 2, guid)

    def can_set_item(self, item, slot):
        if item:
            if 0 > slot > self.max_slot:
                return False
            if not self.is_backpack and len(self.sorted_slots) == self.total_slots:
                return False
            return True
        return False

    def set_item(self, item, slot, count=1):
        if self.can_set_item(item, slot):
            if isinstance(item, ItemManager):
                item_mgr = item
                if item_mgr == self:
                    return None
            else:
                item_mgr = ItemManager.generate_item(item, self.owner, self.current_slot, slot, count=count)

            if item_mgr:
                item_mgr.current_slot = slot
                self.sorted_slots[slot] = item_mgr
                RealmDatabaseManager.character_inventory_update_item(item_mgr.item_instance)

            if item_mgr.item_template.bonding == ItemBondingTypes.BIND_WHEN_PICKED_UP:
                item_mgr.set_binding(True)
            return item_mgr
        return None

    def add_item(self, item_template, count, check_existing=True):
        amount_left = count
        if not self.can_contain_item(item_template):
            return amount_left

        # Check occupied slots for stacking
        if check_existing:
            amount_left = self.add_item_to_existing_stacks(item_template, amount_left)

        if amount_left > 0:
            for x in range(self.start_slot, self.max_slot):
                if x in self.sorted_slots:
                    continue  # Skip any reserved slots
                if not self.is_full():
                    if amount_left > item_template.stackable:
                        self.set_item(item_template, self.next_available_slot(), item_template.stackable)

                        amount_left -= item_template.stackable
                    else:
                        self.set_item(item_template, self.next_available_slot(), amount_left)
                        amount_left = 0
                        break
        return amount_left

    def add_item_to_existing_stacks(self, item_template, count):
        amount_left = count
        if not self.can_contain_item(item_template):
            return amount_left

        # Check occupied slots for stacking
        for x in range(self.start_slot, self.start_slot + self.total_slots):
            if x not in self.sorted_slots:
                continue  # Skip any empty slots
            item_mgr = self.sorted_slots[x]
            if item_mgr.item_template.entry == item_template.entry and \
                    item_mgr.item_instance.stackcount < item_mgr.item_template.stackable:
                stack_missing = item_template.stackable - item_mgr.item_instance.stackcount
                if stack_missing >= amount_left:
                    item_mgr.item_instance.stackcount += amount_left
                    amount_left = 0
                    RealmDatabaseManager.character_inventory_update_item(item_mgr.item_instance)
                    break
                else:
                    item_mgr.item_instance.stackcount += stack_missing
                    amount_left -= stack_missing
                    RealmDatabaseManager.character_inventory_update_item(item_mgr.item_instance)
        return amount_left

    def contains_item(self, item_template):
        if not self.can_contain_item(item_template):
            return False
        for x in range(self.start_slot, self.start_slot + self.total_slots):
            item_mgr = self.sorted_slots[x]
            if item_mgr.item_template.entry == item_template.entry:
                return True
        return False

    def get_item(self, slot):
        if slot in self.sorted_slots:
            return self.sorted_slots[slot]
        return None

    def remove_item(self, item):
        if item:
            return self.remove_item_in_slot(item.current_slot)
        return False

    def remove_item_in_slot(self, slot):
        if slot in self.sorted_slots:
            self.sorted_slots.pop(slot)
            return True
        return False

    def next_available_slot(self):
        for slot in range(self.start_slot, self.start_slot + self.total_slots):
            if slot not in self.sorted_slots:
                return slot
        return -1

    def get_empty_slots(self):
        if self.is_backpack:
            item_count = 0
            for bag_slot in range(InventorySlots.SLOT_ITEM_START, InventorySlots.SLOT_ITEM_END):
                if bag_slot in self.sorted_slots:
                    item_count += 1
            return self.total_slots - item_count
        else:
            return self.total_slots - len(self.sorted_slots)

    def is_full(self):
        return self.get_empty_slots() == 0

    def is_empty(self):
        if self.is_backpack:
            for bag_slot in range(InventorySlots.SLOT_ITEM_START, InventorySlots.SLOT_ITEM_END):
                if bag_slot in self.sorted_slots:
                    return False
            return True
        else:
            return len(self.sorted_slots) == 0

    def can_contain_item(self, item_template):
        if self.is_backpack or self.item_template.class_ == ItemClasses.ITEM_CLASS_CONTAINER:
            return True

        # Must be quiver/ammo pouch
        if self.item_template.subclass == ItemSubClasses.ITEM_SUBCLASS_QUIVER:
            return item_template.bag_family == BagFamilies.ARROWS
        return item_template.bag_family == BagFamilies.BULLETS

    # override
    def get_type(self):
        return ObjectTypes.TYPE_CONTAINER

    # override
    def get_type_id(self):
        return ObjectTypeIds.ID_CONTAINER
