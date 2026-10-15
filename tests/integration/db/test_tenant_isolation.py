"""Изоляция салонов: через репозиторий салон видит только свои строки."""

from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Client, Salon
from beauty_agent.db.repositories.base import SalonRepository


class ClientRepository(SalonRepository[Client]):
    """Настоящий репозиторий клиентов появится 23 октября; для теста хватает базового."""

    model = Client


async def test_salon_does_not_see_other_salon_clients(session: AsyncSession, salon: Salon, other_salon: Salon) -> None:
    anna = ClientRepository(session, salon.id).add(Client(phone="+79161502239", name="Анна"))
    boris = ClientRepository(session, other_salon.id).add(Client(phone="+79161502240", name="Борис"))
    await session.commit()

    clients = ClientRepository(session, salon.id)
    assert await clients.get(anna.id) is anna
    assert await clients.get(boris.id) is None  # чужого клиента не найти даже по точному id
    assert await clients.count() == 1


async def test_repository_sets_its_own_salon(session: AsyncSession, salon: Salon, other_salon: Salon) -> None:
    client = ClientRepository(session, salon.id).add(Client(salon_id=other_salon.id, name="Анна"))

    assert client.salon_id == salon.id  # salon_id из вызывающего кода не принимается
