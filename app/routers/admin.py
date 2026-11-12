from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from ..models import Category, Ticket, User
from ..schemas import CategoryCreate, CategoryPatch, UserCreate, UserPatch, UserPublic
from ..security import Admin, CurrentUser, DB, passwords
from ..services.tickets import change, validate_assignee
from ..realtime import queue

router = APIRouter(prefix="/api", tags=["administration"])


@router.get("/categories")
def categories(db: DB, user: CurrentUser):
    query = select(Category).order_by(Category.sort_order, Category.id)
    if user.role != "ADMIN":
        query = query.where(Category.is_active)
    return db.scalars(query).all()


@router.post("/categories", status_code=201)
def create_category(data: CategoryCreate, db: DB, user: Admin):
    validate_assignee(db, data.default_assignee_id)
    if db.scalar(select(Category).where(Category.name == data.name)):
        raise HTTPException(409, "Такая категория уже существует")
    item = Category(**data.model_dump())
    db.add(item)
    db.commit()
    return item


@router.patch("/categories/{category_id}")
def patch_category(category_id: int, data: CategoryPatch, db: DB, user: Admin):
    item = db.get(Category, category_id)
    if not item:
        raise HTTPException(404, "Категория не найдена")
    values = data.model_dump(exclude_unset=True)
    if any(v is None for k, v in values.items() if k != "default_assignee_id"):
        raise HTTPException(422, "Поле не может быть пустым")
    if "default_assignee_id" in values:
        validate_assignee(db, values["default_assignee_id"])
    if "name" in values and db.scalar(
        select(Category).where(
            Category.name == values["name"], Category.id != category_id
        )
    ):
        raise HTTPException(409, "Такая категория уже существует")
    for key, value in values.items():
        setattr(item, key, value)
    db.commit()
    return item


@router.get("/users", response_model=list[UserPublic])
def users(db: DB, user: Admin):
    return db.scalars(select(User).order_by(User.id)).all()


@router.get("/assignees", response_model=list[UserPublic])
def assignees(db: DB, user: CurrentUser):
    # Separate read-only directory; the full user administration stays ADMIN-only.
    return db.scalars(
        select(User).where(User.is_active, User.role.in_(["MANAGER", "ADMIN"]))
    ).all()


@router.post("/users", response_model=UserPublic, status_code=201)
def create_user(data: UserCreate, db: DB, user: Admin):
    if db.scalar(select(User).where(User.email == data.email.lower())):
        raise HTTPException(409, "Этот email уже используется")
    values = data.model_dump(exclude={"password"})
    values["email"] = data.email.lower()
    item = User(**values, password_hash=passwords.hash(data.password))
    db.add(item)
    db.commit()
    return item


@router.patch("/users/{user_id}", response_model=UserPublic)
def patch_user(user_id: int, data: UserPatch, db: DB, user: Admin):
    item = db.get(User, user_id)
    if not item:
        raise HTTPException(404, "Пользователь не найден")
    values = data.model_dump(exclude_unset=True)
    access_changed = any(key in values and values[key] != getattr(item, key)
                         for key in ("role", "department", "is_active"))
    if any(v is None for v in values.values()):
        raise HTTPException(422, "Поле не может быть пустым")
    if item.id == user.id and (
        values.get("is_active") is False or values.get("role", "ADMIN") != "ADMIN"
    ):
        raise HTTPException(
            422, "Нельзя отключить или понизить собственную учётную запись"
        )
    if "email" in values:
        values["email"] = values["email"].lower()
        if db.scalar(
            select(User).where(User.email == values["email"], User.id != user_id)
        ):
            raise HTTPException(409, "Этот email уже используется")
    if "password" in values:
        item.password_hash = passwords.hash(values.pop("password"))
    for key, value in values.items():
        setattr(item, key, value)
    if not item.is_active or item.role == "EMPLOYEE":
        for ticket in db.scalars(select(Ticket).where(
            Ticket.assignee_id == item.id, Ticket.status != "CLOSED"
        )):
            change(db, ticket, user, "assignee_id", None)
        for category in db.scalars(select(Category).where(
            Category.default_assignee_id == item.id
        )):
            category.default_assignee_id = None
    if access_changed:
        queue(db, {item.id}, {"type": "access_changed"})
    db.commit()
    return item
