"""Membership joins shared by every retrieval statement."""

from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.sql.elements import ColumnElement

from cda.db.models import Pack, TeamMember


def visible_pack_clause(user_id: UUID) -> ColumnElement[bool]:
    return and_(TeamMember.team_id == Pack.team_id, TeamMember.user_id == user_id)
