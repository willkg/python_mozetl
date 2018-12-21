""" Reformat Data for Topline Dashboard

The dashboard can be found at the following url:
http://metrics.services.mozilla.com/firefox-dashboard/

This script replaces `run.sh` and `v4_reformat.py` that generates data
ready for dashboard consumption. The original system used a pipeline
consisting of a custom heka processor, a redshift database, a SQL
roll-up, and a reformatting and upload script. The original data
(referenced as historical data in this module) can be found under the
dashboard at `data/v4-weekly.csv` and `data/v4-monthly.csv`. This is
replaced by the ToplineSummaryView in telemetry-batch-view and this
topline_dashboard script, allowing this process to feed directly from
main_summary.

This script assumes that any failures can be regenerated starting with the
original data backed-up in `telemetry-parquet/topline_summary/v1`. This is all
roll-ups up to the swap-over.
"""

import logging

import click
from pyspark.sql import SparkSession, functions as F

from mozetl import utils
from mozetl.topline.schema import topline_schema, historical_schema

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Limit the countries of interest to the following
countries = set(['US', 'CA', 'BR', 'MX', 'FR', 'ES', 'IT', 'PL',
                 'TR', 'RU', 'DE', 'IN', 'ID', 'CN', 'JP', 'GB'])


def format_spark_path(bucket, prefix):
    """ Generate uri for accessing data on s3 """
    return "s3://{}/{}".format(bucket, prefix)


def reformat_data(df):
    """ Normalize the dataset and aggregate all possible combinations
    of attributes.

        In particular, this will limit the set of values for `geo`, and
    aggregate over every (geo, channel, os) combination. Empty rows are
    pruned since they don't contain extra information about the
    aggregates. This dataframe conforms to the historical dataset
    generated by the `v4_reformat.py` script that is part of the original
    reporting pipeline.
    """

    # A dictionary mapping the column names to the select sub-expression. For
    # most fields, the subexpr ends up being the column name.
    topline_columns = {name: name for name in topline_schema.names}

    # Bucket results by the top k countries, and move the rest into other.
    topline_columns['geo'] = (
        F.when(F.col('geo').isin(list(countries)), F.col('geo'))
         .otherwise('Other')
         .alias('geo')
    )

    # Use the historical dataset's date format, drop the old date column
    topline_columns['date'] = (
        F.from_unixtime(
            F.unix_timestamp(F.col('report_start'), 'yyyyMMdd'),
            'yyyy-MM-dd')
        .alias('date')
    )
    topline_columns.pop('report_start', None)

    # The set of rows in the topline_summary can be categorized into
    # two general groups, attributes and aggregates. Attributes are
    # categorical in nature, while aggregates are numerical.
    attributes = set(['geo', 'channel', 'os', 'date'])
    aggregates = set(topline_columns.keys()) - attributes

    # Cube the results. It's difficult to concisely express
    # partitioning over a single attribute, and cube each subset of
    # attributes. Instead, ignore rows where the date field is empty,
    # which equates to ignoring aggregates over multiple dates.
    cubed_df = (
        df
        .select(*list(topline_columns.values()))
        .cube(*attributes)
        .agg(*[F.sum(F.col(x)).alias(x) for x in aggregates])
        .where(F.col('date').isNotNull())
        .na.fill('all')  # the only fills in string fields
    )

    # Remove rows where the aggregates fields are all zeroes, since
    # these take up extra space in the csv file.
    filtered_df = (
        cubed_df
        .withColumn('total', sum([F.col(x) for x in aggregates]))
        .where(F.col('total') > 0.0)
    )

    # Generate select subexpressions based on the historical
    # schema. Columns missing in the topline_summary from the
    # historical dataset are aggregates, so they are are filled with
    # zeroes.
    def default_column(name):
        return F.lit(0).alias(name)

    column_expr = [name if name in topline_columns else default_column(name)
                   for name in historical_schema.names]
    formatted_df = filtered_df.select(*column_expr)

    return formatted_df


def write_dashboard_data(df, bucket, prefix, mode):
    """ Write the dashboard data to a s3 location. """
    # name of the output key
    key = "{}/topline-{}.csv".format(prefix, mode)
    utils.write_csv_to_s3(df, bucket, key)


@click.command()
@click.argument('mode', type=click.Choice(['weekly', 'monthly']))
@click.argument('bucket')
@click.argument('prefix')
@click.option('--input_bucket',
              default='telemetry-parquet',
              help='Bucket of the ToplineSummary dataset')
@click.option('--input_prefix',
              default='topline_summary/v1',
              help='Prefix of the ToplineSummary dataset')
def main(mode, bucket, prefix, input_bucket, input_prefix):
    spark = (SparkSession
             .builder
             .appName("topline_dashboard")
             .getOrCreate())

    logger.info('Generating {} topline_dashboard'.format(mode))

    # the inclusion of mode doesn't matter, but we need report_start
    input_path = format_spark_path(
        input_bucket,
        "{}/mode={}".format(input_prefix, mode)
    )
    logger.info('Reading input data from {}'.format(input_path))

    # Note: The schema is applied to cast report_start to a string. The
    # DataFrameReader interface has a `.schema()` that mysteriously fails
    # in this context, so the schema is applied after reading the dataframe
    # into memory.
    topline_summary = (
        spark.read
        .parquet(input_path)
        .rdd.toDF(topline_schema)
    )

    # modified topline_summary
    dashboard_data = reformat_data(topline_summary)

    # write this data to the dashboard location
    write_dashboard_data(dashboard_data, bucket, prefix, mode)

    spark.stop()


if __name__ == '__main__':
    main()
