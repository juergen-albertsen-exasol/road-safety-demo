-- Exasol Kafka connector extension (MIT, github.com/exasol/kafka-connector-extension), version 2.0.1.
-- Needs the JAVA script language container (`exasol slc install java -d road-safety-demo`, then a
-- restart) and the JAR in BucketFS. Local deployments have no BucketFS HTTP access: the JAR is
-- copied into the deployment directory, see scripts/setup_kafka.py.
--
-- The UDF sandbox has no DNS (empty resolv.conf and hosts, exasol/exasol-personal#464), so the
-- sidecar name `kafka` doesn't resolve there. The imports name Kafka by its IP instead, and Kafka
-- advertises that IP (scripts/setup_kafka.py, scripts/db.py).
--
-- The script names must not change: KAFKA_CONSUMER generates the import, KAFKA_METADATA reads the
-- highest KAFKA_OFFSET per partition from the target table, KAFKA_IMPORT reads the topic from there.
CREATE SCHEMA IF NOT EXISTS KAFKA_EXTENSION;

CREATE OR REPLACE JAVA SET SCRIPT KAFKA_EXTENSION.KAFKA_CONSUMER(...) EMITS (...) AS
  %scriptclass com.exasol.cloudetl.kafka.KafkaConsumerQueryGenerator;
  %jar /buckets/bfsdefault/default/kafka/exasol-kafka-connector-extension-2.0.1.jar;
/

CREATE OR REPLACE JAVA SET SCRIPT KAFKA_EXTENSION.KAFKA_IMPORT(...) EMITS (...) AS
  %scriptclass com.exasol.cloudetl.kafka.KafkaTopicDataImporter;
  %jar /buckets/bfsdefault/default/kafka/exasol-kafka-connector-extension-2.0.1.jar;
/

CREATE OR REPLACE JAVA SET SCRIPT KAFKA_EXTENSION.KAFKA_METADATA(
  params VARCHAR(2000),
  kafka_partition DECIMAL(18, 0),
  kafka_offset DECIMAL(36, 0)
)
EMITS (partition_index DECIMAL(18, 0), max_offset DECIMAL(36, 0)) AS
  %scriptclass com.exasol.cloudetl.kafka.KafkaTopicMetadataReader;
  %jar /buckets/bfsdefault/default/kafka/exasol-kafka-connector-extension-2.0.1.jar;
/
