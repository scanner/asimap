#!/usr/bin/env python
#
"""
Factories for various objects in the ASIMAP server.
"""
# system imports
#
from typing import Any, Sequence

# 3rd party imports
#
import factory
from factory import post_generation
from faker import Faker

# project imports
#
from ..auth import User
from ..hashers import make_password

# factory.Faker() is good and all but it delays evaluation and returns a Faker
# instance. Sometimes we just want a fake value now when the object is
# constructed.
#
fake = Faker()


########################################################################
########################################################################
#
class UserFactory(factory.Factory):
    class Meta:
        model = User

    username = factory.Faker("email")
    maildir = factory.LazyAttribute(lambda o: f"/var/tmp/maildirs/{o.username}")
    password_hash = "!invalid_pw"  # NOTE: Fixed in post_generation below

    @post_generation
    def password(self, create: bool, extracted: Sequence[Any], **kwargs):
        password = extracted if extracted else fake.password(length=16)
        self.pw_hash = make_password(password)
