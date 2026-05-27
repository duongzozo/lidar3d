"""Create admin user — run inside backend container.
Usage: docker compose exec -it backend python scripts/create_admin.py
"""
import asyncio
import sys
import uuid

sys.path.insert(0, "/app")


async def main() -> None:
    from app.core.security import hash_password
    from app.db.session import AsyncSessionFactory
    from app.models.models import User, UserRole

    email    = input("Email   : ").strip()
    username = input("Username: ").strip() or email.split("@")[0]
    password = input("Password: ").strip()

    if not email or not password:
        print("ERROR: email and password are required")
        sys.exit(1)

    async with AsyncSessionFactory() as db:
        u = User(
            id=uuid.uuid4(),
            email=email,
            username=username,
            hashed_password=hash_password(password),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(u)
        await db.commit()
        print(f"\nAdmin created successfully: {email}")


asyncio.run(main())
