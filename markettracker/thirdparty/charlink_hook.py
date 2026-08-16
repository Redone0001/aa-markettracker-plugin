# Third Party
from charlink.app_imports.utils import AppImport, LoginImport

# Django
from django.contrib.auth.models import Permission
from django.db.models import Exists, OuterRef

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership, User
from allianceauth.eveonline.models import EveCharacter

# MarketTracker
from markettracker.models import MarketCharacter


def _add_character_markettracker(request, token):
    """
    Add character to MarketTracker as normal user login only.
    IMPORTANT:
    - This does NOT create/admin-link market admin characters.
    - If character already exists in MarketTracker, do nothing.
    """
    eve_character, _ = EveCharacter.objects.get_or_create(
        character_id=token.character_id,
        defaults={"character_name": token.character_name},
    )

    ownership, _ = CharacterOwnership.objects.get_or_create(
        character=eve_character,
        user=request.user,
    )

    # If already linked, do not overwrite type/admin state
    if MarketCharacter.objects.filter(character=ownership).exists():
        return

    MarketCharacter.objects.create(
        character=ownership,
        token=token,
        type="user",
    )


def _is_character_added_markettracker(character: EveCharacter):
    """
    Character is considered already added if it already exists in MarketTracker,
    regardless of whether it is user/admin type.
    This prevents CharLink from overwriting an existing admin linkage.
    """
    return MarketCharacter.objects.filter(character__character=character).exists()


def _users_with_perms_markettracker():
    """
    Mirrors AllianceAuth permission resolution:
    - direct user permission
    - group permission
    - state permission
    - superusers
    """
    permission = Permission.objects.get(
        content_type__app_label="markettracker",
        codename="basic_access",
    )

    users_qs = (
        permission.user_set.all()
        | User.objects.filter(
            groups__in=list(permission.group_set.values_list("pk", flat=True))
        )
        | User.objects.select_related("profile").filter(
            profile__state__in=list(permission.state_set.values_list("pk", flat=True))
        )
        | User.objects.filter(is_superuser=True)
    )
    return users_qs.distinct()


app_import = AppImport(
    "markettracker",
    [
        LoginImport(
            app_label="markettracker",
            unique_id="contractsuser",
            field_label="MarketTracker",
            add_character=_add_character_markettracker,
            scopes=[
                "esi-contracts.read_character_contracts.v1",
                "esi-assets.read_assets.v1",
                "esi-markets.read_character_orders.v1",
            ],
            check_permissions=lambda user: user.has_perm("markettracker.basic_access"),
            is_character_added=_is_character_added_markettracker,
            is_character_added_annotation=Exists(
                MarketCharacter.objects.filter(character__character_id=OuterRef("pk"))
            ),
            get_users_with_perms=_users_with_perms_markettracker,
        ),
    ],
)