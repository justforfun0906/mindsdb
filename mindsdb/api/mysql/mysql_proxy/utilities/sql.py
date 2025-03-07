import copy

import duckdb
import numpy as np

from mindsdb_sql import parse_sql
from mindsdb_sql.render.sqlalchemy_render import SqlalchemyRender
from mindsdb_sql.planner.utils import query_traversal
from mindsdb_sql.parser.ast import (
    Select, Identifier,
    Function, Constant
)

from mindsdb.utilities import log
from mindsdb.utilities.json_encoder import CustomJSONEncoder


def query_df(df, query, session=None):
    """ Perform simple query ('select' from one table, without subqueries and joins) on DataFrame.

        Args:
            df (pandas.DataFrame): data
            query (mindsdb_sql.parser.ast.Select | str): select query

        Returns:
            pandas.DataFrame
    """

    if isinstance(query, str):
        query_ast = parse_sql(query, dialect='mysql')
    else:
        query_ast = copy.deepcopy(query)

    if isinstance(query_ast, Select) is False \
       or isinstance(query_ast.from_table, Identifier) is False:
        raise Exception(
            "Only 'SELECT from TABLE' statements supported for internal query"
        )

    table_name = query_ast.from_table.parts[0]
    query_ast.from_table.parts = ['df_table']

    json_columns = set()

    def adapt_query(node, is_table, **kwargs):
        if is_table:
            return
        if isinstance(node, Identifier):
            if len(node.parts) > 1:
                node.parts = [node.parts[-1]]
                return node
        if isinstance(node, Function):
            fnc_name = node.op.lower()
            if fnc_name == 'database' and len(node.args) == 0:
                if session is not None:
                    cur_db = session.database
                else:
                    cur_db = None
                return Constant(cur_db)
            if fnc_name == 'truncate':
                # replace mysql 'truncate' function to duckdb 'round'
                node.op = 'round'
                if len(node.args) == 1:
                    node.args.append(0)
            if fnc_name == 'json_extract':
                json_columns.add(node.args[0].parts[-1])

    query_traversal(query_ast, adapt_query)

    # convert json columns
    encoder = CustomJSONEncoder()

    def _convert(v):
        if isinstance(v, dict) or isinstance(v, list):
            try:
                return encoder.encode(v)
            except Exception:
                pass
        return v
    for column in json_columns:
        df[column] = df[column].apply(_convert)

    render = SqlalchemyRender('postgres')
    try:
        query_str = render.get_string(query_ast, with_failback=False)
    except Exception as e:
        log.logger.error(
            f"Exception during query casting to 'postgres' dialect. Query: {str(query)}. Error: {e}"
        )
        query_str = render.get_string(query_ast, with_failback=True)

    # workaround to prevent duckdb.TypeMismatchException
    if len(df) > 0 and table_name.lower() in ('models', 'predictors'):
        if 'TRAINING_OPTIONS' in df.columns:
            df = df.astype({'TRAINING_OPTIONS': 'string'})

    con = duckdb.connect(database=':memory:')
    con.register('df_table', df)
    result_df = con.execute(query_str).fetchdf()
    result_df = result_df.replace({np.nan: None})
    description = con.description
    con.unregister('df_table')
    con.close()

    new_column_names = {}
    real_column_names = [x[0] for x in description]
    for i, duck_column_name in enumerate(result_df.columns):
        new_column_names[duck_column_name] = real_column_names[i]
    result_df = result_df.rename(
        new_column_names,
        axis='columns'
    )
    return result_df
