from tune.protox.cli import protox_group
from tune.protox.embedding.cli import embedding_group

protox_group.add_command(embedding_group)