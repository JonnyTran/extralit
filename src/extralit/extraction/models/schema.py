import logging
import os
from collections import deque
from glob import glob
from io import BytesIO
from typing import List, Optional, Union, Dict

import pandera as pa
from minio import Minio
from pandera.api.base.model import MetaModel
from pandera.io import from_json, from_yaml
from pydantic.v1 import BaseModel, Field, validator

DEFAULT_SCHEMA_S3_PATH = 'schemas/'

_LOGGER = logging.getLogger(__name__)

class SchemaStructure(BaseModel):
    schemas: List[pa.DataFrameSchema] = Field(default_factory=list)
    document_schema: Optional[pa.DataFrameSchema] = None

    @validator('schemas', pre=True, each_item=True)
    def parse_schema(cls, v: Union[pa.DataFrameModel, pa.DataFrameSchema]):
        return v.to_schema() if hasattr(v, 'to_schema') else v

    @validator('document_schema', pre=True)
    def parse_document_schema(cls, v: Union[pa.DataFrameModel, pa.DataFrameSchema]):
        schema: pa.DataFrameSchema = v.to_schema() if hasattr(v, 'to_schema') else v
        assert all(key.islower() for key in schema.columns.keys()), f"All keys in {schema.name} schema must be lowercased"
        return schema

    @classmethod
    def from_dir(cls, dir_path: str, exclude: List[str]=[]):
        """
        Load a SchemaStructure from a directory containing pandera DataFrameSchema .json files.
        Args:
            dir_path: A directory path containing pandera DataFrameSchema .json files.
            exclude: A list of schema names to exclude from the schema structure.

        Returns:
            SchemaStructure
        """
        schemas = {}
        if os.path.isdir(dir_path):
            schema_paths = sorted(glob(os.path.join(dir_path, '*.json')), key=lambda x: not x.endswith('.json'))
        else:
            schema_paths = sorted(glob(dir_path), key=lambda x: not x.endswith('.json'))

        for filepath in schema_paths:
            try:
                if filepath.endswith('.json'):
                    schema = from_json(filepath)
                elif filepath.endswith('.yaml') or filepath.endswith('.yml'):
                    schema = from_yaml(filepath)
                else:
                    continue

                if schema.name in schemas or schema.name in exclude:
                    continue

                schemas[schema.name] = schema
            except Exception as e:
                _LOGGER.warning(f"Ignoring failed schema loading from '{filepath}': \n{e}")

        return cls(schemas=list(schemas.values()))

    @classmethod
    def from_s3(cls, workspace: str, minio_client: Minio, prefix: str = DEFAULT_SCHEMA_S3_PATH,
                exclude: List[str] = [], verbose: bool = True):
        """
        Load a SchemaStructure from a Minio bucket containing pandera DataFrameSchema .json files.

        Args:
            workspace:  The workspace name.
            minio_client:  The Minio client.
            prefix: The prefix to search for schemas.
            exclude:  A list of schema names to exclude from the schema structure.
            verbose:  Whether to log verbose output.

        Returns:
            SchemaStructure
        """
        schemas = {}
        objects = minio_client.list_objects(workspace, prefix=prefix, include_version=False)

        # Sort the objects by file extension
        objects = sorted(objects, key=lambda obj: (
            os.path.splitext(obj.object_name)[1] != '', os.path.splitext(obj.object_name)[1]))

        for obj in objects:
            filepath = obj.object_name
            file_extension = os.path.splitext(filepath)[1]

            try:
                data = minio_client.get_object(workspace, filepath)
                file_data = BytesIO(data.read())

                if not file_extension or file_extension == '.json':
                    schema = from_json(file_data)
                elif file_extension in ['.yaml', '.yml']:
                    schema = from_yaml(file_data)
                else:
                    continue

                if schema.name in schemas or schema.name in exclude:
                    continue

                _LOGGER.info(f'Loaded {schema.name} from {filepath}', exc_info=1)
                schemas[schema.name] = schema
            except Exception as e:
                _LOGGER.warning(f"Ignoring failed schema loading from '{filepath}': \n{e}")

        return cls(schemas=list(schemas.values()))

    def to_s3(self, workspace: str, minio_client: Minio, prefix: str = 'schemas/', delete_excluded: bool = False):
        """
        This method is used to upload the schemas to an S3 bucket and optionally delete the excluded schemas.

        Args:
            workspace (str): The workspace name.
            minio_client (Minio): The Minio client.
            prefix (str, optional): The prefix to use for the schemas in the S3 bucket. Default is 'schemas/'.
            delete_excluded (bool, optional): A flag to determine whether to delete the excluded schemas or not. Default is True.

        Returns:
            None
        """

        for schema in self.schemas:
            # Serialize the schema to a JSON string
            schema_json = schema.to_json()

            # Create a BytesIO object from the JSON string
            schema_bytes = BytesIO(schema_json.encode())

            # Define the object name
            object_name = os.path.join(prefix, schema.name)

            # Upload the BytesIO object to the S3 bucket
            minio_client.put_object(
                bucket_name=workspace,
                object_name=object_name,
                data=schema_bytes,
                length=schema_bytes.getbuffer().nbytes,
                content_type='application/json'
            )

        if delete_excluded:
            objects = minio_client.list_objects(workspace, prefix=prefix, include_version=False)
            bucket_schema_paths = [os.path.splitext(obj.object_name)[0] for obj in objects]
            self_schema_paths = [os.path.join(prefix, schema.name) for schema in self.schemas]
            schemas_to_delete = set(bucket_schema_paths) - set(self_schema_paths)
            print('Deleting schemas:', schemas_to_delete)
            for schema_path in schemas_to_delete:
                minio_client.remove_object(workspace, schema_path)

    def get_joined_schema(self, schema_name: str):
        combined_columns = {}
        combined_checks = []

        # Iterate over the provided schema and its dependent schemas
        dependent_schemas: List[pa.DataFrameSchema] = [
            self.__getitem__(sn) for sn in self.upstream_dependencies.get(schema_name)
        ]

        for schema in [self.__getitem__(schema_name)] + dependent_schemas:
            for column_name, column_schema in schema.columns.items():
                if column_name not in combined_columns:
                    combined_columns[column_name] = column_schema

            combined_checks.extend(schema.checks)

        joined_schema = pa.DataFrameSchema(columns=combined_columns, checks=combined_checks, name=schema_name)
        return joined_schema

    @property
    def downstream_dependencies(self) -> Dict[str, List[str]]:
        dependents = {}
        for schema in self.schemas:
            dependents[schema.name] = []
            schema_index_names = self.index_names(schema)

            for dep in self.schemas:
                if not dep.index or schema == dep: continue
                dep_index_names = self.index_names(dep)
                if f"{schema.name}_ref".lower() in dep_index_names:
                    dependents[schema.name].append(dep.name)

                if schema.index and f"{schema.name}_ID" in dep_index_names and f"{schema.name}_ID" in schema_index_names:
                    dependents[schema.name].append(dep.name)
        return dependents

    @property
    def upstream_dependencies(self) -> Dict[str, List[str]]:
        dependencies = {}
        for schema in self.schemas:
            dependencies[schema.name] = []
            schema_index_names = self.index_names(schema)

            for dep in self.schemas:
                if not schema.index or schema == dep: continue
                dep_index_names = self.index_names(dep)
                if f"{dep.name}_ref".lower() in schema_index_names:
                    dependencies[schema.name].append(dep.name)

                if dep.index and f"{dep.name}_ID" in schema_index_names and f"{dep.name}_ID" in dep_index_names:
                    dependencies[schema.name].append(dep.name)
        return dependencies

    def index_names(self, schema: Union[str, pa.DataFrameSchema]) -> List[str]:
        schema = self.__getitem__(schema) if isinstance(schema, str) else schema
        if not schema.index: return []
        index_names = list(schema.index.names or [schema.index.name])
        index_names = [name for name in index_names if name]
        return index_names

    def columns(self, schema: str) -> List[str]:
        columns = list(self.__getitem__(schema).columns)
        return columns

    @property
    def ordering(self) -> List[str]:
        visited = {schema.name: 0 for schema in self.schemas}
        stack = deque()

        for schema in self.schemas:
            if visited[schema.name] == 0:
                # If the node is white, visit it
                topological_sort(schema.name, visited, stack, self.downstream_dependencies)

        return list(stack)

    def __iter__(self):
        return iter(self.ordering)

    def __getitem__(self, item: str):
        if isinstance(item, pa.DataFrameSchema):
            item = item.name
        elif isinstance(item, MetaModel):
            item = str(item)

        for schema in self.schemas:
            if schema.name.lower() == item.lower():
                return schema
        raise KeyError(f"No schema found for '{item}'")

    def __repr_args__(self):
        args = [(s.name, ((s.index.names or [s.index.name]) if s.index else []) + list(s.columns)) \
                for s in self.schemas]
        return args

    class Config:
        arbitrary_types_allowed = True


def topological_sort(schema_name: str, visited: Dict[str, int], stack: deque,
                     dependencies: Dict[str, List[str]]) -> None:
    visited[schema_name] = 1  # Gray

    for i in dependencies.get(schema_name, []):
        if visited[i] == 1:  # If the node is gray, it means we have a cycle
            raise ValueError(f"Circular dependency detected: {schema_name} depends on {i} and vice versa")
        if visited[i] == 0:  # If the node is white, visit it
            topological_sort(i, visited, stack, dependencies)

    visited[schema_name] = 2  # Black
    stack.appendleft(schema_name)
