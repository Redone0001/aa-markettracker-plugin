from allianceauth.authentication.models import CharacterOwnership
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from esi.models import Token

try:
    from fittings.models import Fitting
    HAS_FITTINGS = True
except Exception:
    Fitting = None
    HAS_FITTINGS = False


class DiscordWebhook(models.Model):
    """
    Global webhook.
    """
    name = models.CharField(max_length=100, default="Webhook")
    url = models.URLField(unique=True)

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.name} – {self.url}"


class DiscordMessage(models.Model):
    """
    Messages settings
    """
    item_alert_enabled = models.BooleanField(default=True)
    item_restocked_alert_enabled = models.BooleanField(default=True)
    contract_alert_enabled = models.BooleanField(default=True)
    contract_restocked_alert_enabled = models.BooleanField(default=True)

    # headers
    item_alert_header = models.CharField(
        max_length=200,
        default="⚠️ MarketTracker Items",
        help_text=_("Header used for items alerts"),
    )
    contract_alert_header = models.CharField(
        max_length=200,
        default="📦 MarketTracker Contracts",
        help_text=_("Header used for contract alerts"),
    )
    item_restocked_alert_header = models.CharField(
        max_length=200,
        default="👍 Items back in stock",
        help_text=_("Header used for items restock alerts"),
    )

    # contract restocked header
    contract_restocked_alert_header = models.CharField(
        max_length=200,
        default="✅ Contracts restocked",
        help_text=_("Header used for contracts restock alerts"),
    )

    # ping target for items
    item_ping_choice = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        help_text=_("Special ping target: none, here, everyone"),
    )
    item_ping_group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, null=True, blank=True,
        help_text=_("Discord role (from AA group) to ping for items"),
        related_name="markettracker_item_pings",
    )

    # ping target for contracts
    contract_ping_choice = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        help_text=_("Special ping target: none, here, everyone"),
    )
    contract_ping_group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, null=True, blank=True,
        help_text=_("Discord role (from AA group) to ping for contracts"),
        related_name="markettracker_contract_pings",
    )

    # ping target for items restocked
    item_restocked_ping_choice = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        help_text=_("Special ping target: none, here, everyone"),
    )
    item_restocked_ping_group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, null=True, blank=True,
        help_text=_("Discord role (from AA group) to ping for items restocked"),
        related_name="markettracker_item_restocked_pings",
    )

    # ping target for contracts restocked
    contract_restocked_ping_choice = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        help_text=_("Special ping target: none, here, everyone"),
    )
    contract_restocked_ping_group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, null=True, blank=True,
        help_text=_("Discord role (from AA group) to ping for contracts restocked"),
        related_name="markettracker_contract_restocked_pings",
    )

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return "Discord Messages / Pings"



class MarketTrackingConfig(models.Model):
    """
    General tracking settings
    """
    SCOPE_CHOICES = [
        ("region", _("Region")),
        ("structure", _("Structure")),
    ]
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default="region")
    # Store the final ID here (region_id or structure_id depending on scope)
    location_id = models.BigIntegerField(verbose_name=_("Region or Structure ID"))

    yellow_threshold = models.PositiveIntegerField(
        default=50,
        verbose_name=_("Yellow status threshold (%)")
    )
    red_threshold = models.PositiveIntegerField(
        default=25,
        verbose_name=_("Red status threshold (%)")
    )

    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.scope} – {self.location_id}"


class MarketCharacter(models.Model):
    character = models.OneToOneField(
        CharacterOwnership, on_delete=models.CASCADE, related_name="market_character"
    )
    token = models.OneToOneField(Token, on_delete=models.CASCADE)
    type = models.CharField(max_length=20, default="user")  # "admin" or "user"

    class Meta:
        default_permissions = ()

    def __str__(self):
        return self.character.character.name


class TrackedLocation(models.Model):
    class Scope(models.TextChoices):
        REGION = "region", _("Region")
        STRUCTURE = "structure", _("Structure")

    # admin label
    name = models.CharField(max_length=255)

    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.STRUCTURE)

    # region_id or structure_id
    location_id = models.BigIntegerField()

    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        default_permissions = ()
        unique_together = (("scope", "location_id"),)

    def __str__(self):
        return f"{self.name} ({self.scope}:{self.location_id})"



class TrackedItem(models.Model):
    item = models.ForeignKey(
        "eveuniverse.EveType", on_delete=models.CASCADE, verbose_name=_("Item")
    )
    desired_quantity = models.IntegerField(default=0, verbose_name=_("Desired quantity"))
    last_status = models.CharField(max_length=10, default="OK", verbose_name=_("Last status"))
    location = models.ForeignKey(
        "markettracker.TrackedLocation",
        on_delete=models.CASCADE,
        related_name="tracked_items",
    )


    class Meta:
        unique_together = ("item", "location")
        default_permissions = ()

    def __str__(self):
        return self.item.name


class MarketOrderSnapshot(models.Model):
    tracked_item = models.ForeignKey(
        "markettracker.TrackedItem",
        on_delete=models.CASCADE,
        related_name="order_snapshots",
    )
    order_id = models.BigIntegerField(unique=True)
    structure_id = models.BigIntegerField()
    price = models.FloatField()
    volume_remain = models.IntegerField()
    is_buy_order = models.BooleanField(default=False)
    issued = models.DateTimeField()

    class Meta:
        default_permissions = ()
        indexes = [
            models.Index(fields=["tracked_item"]),
            models.Index(fields=["structure_id"]),
            models.Index(fields=["is_buy_order"]),
        ]

    def __str__(self):
        return f"{self.tracked_item.item.name} - {self.price}"


class TrackedContract(models.Model):
    class Mode(models.TextChoices):
        CUSTOM = "custom", _("Custom (title match)")
        DOCTRINE = "doctrine", _("Doctrine (fitting match)")

    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.CUSTOM)
    title_filter = models.CharField(max_length=120, blank=True)
    fitting = models.ForeignKey(Fitting, null=True, blank=True, on_delete=models.SET_NULL)
    max_price = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    desired_quantity = models.PositiveIntegerField(default=0)
    last_status = models.CharField(max_length=10, default="OK")

    class Meta:
        default_permissions = ()
        ordering = ["mode", "title_filter"]

    def display_name(self):
        if self.mode == self.Mode.CUSTOM:
            return self.title_filter or "—"
        return getattr(self.fitting, "name", "—")

    def __str__(self):
        if self.mode == self.Mode.CUSTOM:
            return f"[custom] {self.title_filter or '-'}"
        return f"[doctrine] {getattr(self.fitting, 'name', '—')}"


class TrackedContractLocation(models.Model):
    tracked_contract = models.ForeignKey(
        "markettracker.TrackedContract",
        on_delete=models.CASCADE,
        related_name="by_location",
    )
    location = models.ForeignKey(
        "markettracker.TrackedLocation",
        on_delete=models.CASCADE,
        related_name="tracked_contracts",
    )

    desired_quantity = models.PositiveIntegerField(default=0)
    last_status = models.CharField(max_length=10, default="OK")
    is_active = models.BooleanField(default=True)

    class Meta:
        default_permissions = ()
        unique_together = (("tracked_contract", "location"),)

    def __str__(self):
        return f"TC#{self.tracked_contract_id} @ {self.location}"


class ContractSnapshot(models.Model):
    contract_id = models.BigIntegerField(unique=True)
    owner_character_id = models.BigIntegerField(null=False)
    owner_character_name = models.CharField(max_length=128, blank=True, default="")
    type = models.CharField(max_length=32, blank=True, default="")
    availability = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    date_issued = models.DateTimeField(null=True, blank=True)
    date_expired = models.DateTimeField(null=True, blank=True)
    start_location_id = models.BigIntegerField(null=True, blank=True)
    end_location_id = models.BigIntegerField(null=True, blank=True)
    price = models.FloatField(null=True, blank=True)
    reward = models.FloatField(null=True, blank=True)
    collateral = models.FloatField(null=True, blank=True)
    volume = models.FloatField(null=True, blank=True)
    for_corporation = models.BooleanField(default=False)
    assignee_id = models.BigIntegerField(null=True, blank=True)
    acceptor_id = models.BigIntegerField(null=True, blank=True)
    issuer_id = models.BigIntegerField(null=True, blank=True)
    issuer_corporation_id = models.BigIntegerField(null=True, blank=True)
    items = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)
    date_completed = models.DateTimeField(null=True, blank=True)


    class Meta:
        default_permissions = ()

    def __str__(self):
        return f"{self.contract_id} ({self.type}, {self.status})"


class ContractError(models.Model):
    class Code(models.TextChoices):
        PRICE_TOO_HIGH = "price", _("Price exceeds max")
        WRONG_HULL = "hull", _("Wrong hull")
        MODULE_MISMATCH = "modules", _("Modules mismatch")
        TITLE_MISMATCH = "title", _("Title mismatch")
        DOCTRINE_MISSING = "doctrine", _("Doctrine fitting missing")

    tracked = models.ForeignKey(TrackedContract, on_delete=models.CASCADE, related_name="errors")
    contract_id = models.BigIntegerField()
    owner_name = models.CharField(max_length=128, blank=True)
    code = models.CharField(max_length=16, choices=Code.choices)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_resolved = models.BooleanField(default=False)

    class Meta:
        default_permissions = ()
        ordering = ["-created_at"]


class Delivery(models.Model):
    STATUS_CHOICES = [("PENDING", _("Pending")), ("FINISHED", _("Finished"))]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name=_("User"))
    character = models.ForeignKey(
        CharacterOwnership, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("Character")
    )
    item = models.ForeignKey("eveuniverse.EveType", on_delete=models.CASCADE, verbose_name=_("Item"))
    declared_quantity = models.PositiveIntegerField(verbose_name=_("Declared quantity"))
    delivered_quantity = models.PositiveIntegerField(default=0, verbose_name=_("Delivered quantity"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING", verbose_name=_("Status"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def is_finished(self):
        return self.status == "FINISHED" or self.delivered_quantity >= self.declared_quantity

    def __str__(self):
        return f"{self.user} – {self.item} ({self.delivered_quantity}/{self.declared_quantity})"


class ContractDelivery(models.Model):
    STATUS_CHOICES = [("PENDING", _("Pending")), ("FINISHED", _("Finished"))]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name=_("User"))
    tracked_contract = models.ForeignKey(
        "markettracker.TrackedContract", on_delete=models.CASCADE, related_name="deliveries", verbose_name=_("Tracked contract")
    )
    declared_quantity = models.PositiveIntegerField(verbose_name=_("Declared quantity"))
    delivered_quantity = models.PositiveIntegerField(default=0, verbose_name=_("Delivered quantity"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING", verbose_name=_("Status"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def is_finished(self):
        return self.status == "FINISHED" or self.delivered_quantity >= self.declared_quantity

    def __str__(self):
        return f"{self.user} – TC#{self.tracked_contract_id} ({self.delivered_quantity}/{self.declared_quantity})"
    

class MTTaskLog(models.Model):
    created = models.DateTimeField(default=timezone.now, db_index=True)
    level = models.CharField(max_length=10, default="INFO", db_index=True)  # INFO/WARN/ERROR
    source = models.CharField(max_length=64, default="contracts", db_index=True)  # contracts/items/market/etc
    event = models.CharField(max_length=64, default="run", db_index=True)   # run/start/end/esi_error/status_change
    message = models.TextField(blank=True, default="")
    data = models.JSONField(blank=True, null=True)  # Django 3.1+; add an alternative if using an older version

    class Meta:
        ordering = ["-created"]
        default_permissions = ()



class General(models.Model):
    """
    Permissions.
    """
    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", "Can access this app"),
            ("can_manage_stocks", "Can set tracked items"),
            ("can_manage_deliveries", "Can see all finished deliveries and delete current ones"),
        )
