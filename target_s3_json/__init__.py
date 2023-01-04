#!/usr/bin/env python3

import argparse
import csv
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

import singer
from jsonschema import Draft7Validator, FormatChecker

from target_s3_json import s3
from target_s3_json import utils

logger = singer.get_logger()

def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        logger.debug('Emitting state {}'.format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()


# pylint: disable=too-many-locals,too-many-branches,too-many-statements
def persist_messages(messages, config, s3_client):
    state = None
    schemas = {}
    key_properties = {}
    validators = {}

    delimiter = config.get('delimiter', ',')
    # Use the system specific temp directory if no custom temp_dir provided
    temp_dir = os.path.expanduser(config.get('temp_dir', tempfile.gettempdir()))

    # Create temp_dir if not exists
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)

    records = []
    now = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
    logger.info(messages)

    for message in messages:
        try:
            o = singer.parse_message(message).asdict()
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(message))
            raise
        message_type = o['type']
        if message_type == 'RECORD':
            stream_name = o['stream']

            if stream_name not in schemas:
                raise Exception("A record for stream {}"
                                "was encountered before a corresponding schema".format(stream_name))

            # Validate record
            try:
                validators[o['stream']].validate(utils.float_to_decimal(o['record']))
            except Exception as ex:
                if type(ex).__name__ == "InvalidOperation":
                    logger.error("Data validation failed and cannot load to destination. RECORD: {}\n"
                                 "'multipleOf' validations that allows long precisions are not supported"
                                 " (i.e. with 15 digits or more). Try removing 'multipleOf' methods from JSON schema."
                    .format(o['record']))
                    raise ex

            record_to_load = o['record']
            if config.get('add_metadata_columns'):
                record_to_load = utils.add_metadata_values_to_record(o, {})
            else:
                record_to_load = utils.remove_metadata_values_from_record(o)

            records.append(record_to_load)
        # WARN - THESE CONDITIONS ARE NOT DEVELOPED
        elif message_type == 'STATE':
            logger.debug('Setting state to {}'.format(o['value']))
            state = o['value']
        elif message_type == 'SCHEMA':
            if o['stream'] != stream or None:
                file_nr = 0
            stream = o['stream']
            schemas[stream] = o['schema']
            if config.get('add_metadata_columns'):
                schemas[stream] = utils.add_metadata_columns_to_schema(o)

            stream_logname = "streams_{}_{}.log".format(config.get('edwRecordSource'), now)
            with open(stream_logname, 'a') as streams_file:
                streams_file.write(stream + '\n')

            schema = utils.float_to_decimal(o['schema'])
            validators[stream] = Draft7Validator(schema, format_checker=FormatChecker())
            key_properties[stream] = o['key_properties']
        elif message_type == 'ACTIVATE_VERSION':
            logger.debug('ACTIVATE_VERSION message')
        else:
            logger.warning("Unknown message type {} in message {}"
                            .format(o['type'], o))


    filename = os.path.expanduser(os.path.join(temp_dir, stream_name + '-' + now + '.json'))
    target_key = utils.get_target_key(message=o,
                                      prefix=config.get('s3_key_prefix', ''),
                                      timestamp=now,
                                      naming_convention=config.get('naming_convention'))

    # Upload to S3
    with open(filename, 'w') as f:
        f.write(json.dumps(records, indent=4))

    compressed_file = None
    if config.get("compression") is None or config["compression"].lower() == "none":
        pass  # no compression
    else:
        if config["compression"] == "gzip":
            compressed_file = f"{filename}.gz"
            with open(filename, 'rb') as f_in:
                with gzip.open(compressed_file, 'wb') as f_out:
                    logger.info(f"Compressing file as '{compressed_file}'")
                    shutil.copyfileobj(f_in, f_out)
            target_key += ".gz"
        else:
            raise NotImplementedError(
                "Compression type '{}' is not supported. "
                "Expected: 'none' or 'gzip'"
                .format(config["compression"])
            )

    s3.upload_file(compressed_file or filename,
                    s3_client,
                    config.get('s3_bucket'),
                    target_key,
                    encryption_type=config.get('encryption_type'),
                    encryption_key=config.get('encryption_key'))

    # Remove the local file(s)
    os.remove(filename)
    if compressed_file:
        os.remove(compressed_file)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', help='Config file')
    args = parser.parse_args()

    if args.config:
        with open(args.config) as input_json:
            config = json.load(input_json)
    else:
        config = {}

    config_errors = utils.validate_config(config)
    if len(config_errors) > 0:
        logger.error("Invalid configuration:\n   * {}".format('\n   * '.join(config_errors)))
        sys.exit(1)

    s3_client = s3.create_client(config)

    input_messages = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    state = persist_messages(input_messages, config, s3_client)

    emit_state(state)
    logger.debug("Exiting normally")


if __name__ == '__main__':
    main()
