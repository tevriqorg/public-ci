from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, create_db_engine


@pytest.fixture
def session(tmp_path) -> Generator[Session, None, None]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as value:
        yield value
    Base.metadata.drop_all(engine)
    engine.dispose()
