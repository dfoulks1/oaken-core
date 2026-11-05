# SPDX-License-Identifier: Apache-2.0
"""``[plugins.vector_store.<name>]`` for the sqlite-vec store."""

from pydantic import BaseModel, ConfigDict, Field


class SqliteVecConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = ""  # the file; empty means vectors.db in the data folder
    read_pool_size: int = Field(default=4, ge=1, le=64)
