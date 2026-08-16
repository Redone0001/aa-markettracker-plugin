"""Alliance Auth character-ownership integration helpers."""

from allianceauth.authentication.models import CharacterOwnership
from django.core.exceptions import PermissionDenied
from django.db import transaction


@transaction.atomic
def ownership_for_token(*, token, user) -> CharacterOwnership:
    """Return the AA ownership represented by ``token`` for ``user``.

    django-esi normally associates callback tokens with the authenticated user,
    and Alliance Auth creates the ownership from its token signal.  Unowned
    legacy tokens can still be selected by django-esi, so claim those first and
    then use AA's public ``create_by_token`` manager API if the signal did not
    already create the row.
    """
    if token.user_id is None:
        token.user = user
        token.save(update_fields=["user"])
    elif token.user_id != user.pk:
        raise PermissionDenied("The selected ESI token belongs to another user.")

    ownership = CharacterOwnership.objects.filter(
        character__character_id=token.character_id,
        user=user,
        owner_hash=token.character_owner_hash,
    ).first()
    if ownership:
        return ownership

    if CharacterOwnership.objects.filter(
        character__character_id=token.character_id
    ).exists():
        raise PermissionDenied(
            "Alliance Auth has a conflicting ownership record for this character."
        )

    return CharacterOwnership.objects.create_by_token(token)
