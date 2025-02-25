import time
import urllib
from typing import Any, Dict, Optional, cast

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.sql import text

from datahub.cli.docker import check_local_docker_containers
from datahub.ingestion.run.pipeline import Pipeline
from datahub.ingestion.source.sql.mysql import MySQLConfig, MySQLSource
from datahub.ingestion.source.sql.sql_common import BaseSQLAlchemyCheckpointState
from datahub.ingestion.source.state.checkpoint import Checkpoint
from tests.utils import ingest_file_via_rest

GMS_ENDPOINT = "http://localhost:8080"
FRONTEND_ENDPOINT = "http://localhost:9002"
KAFKA_BROKER = "localhost:9092"

bootstrap_sample_data = "../metadata-ingestion/examples/mce_files/bootstrap_mce.json"
usage_sample_data = (
    "../metadata-ingestion/tests/integration/bigquery-usage/bigquery_usages_golden.json"
)
bq_sample_data = "./sample_bq_data.json"
restli_default_headers = {
    "X-RestLi-Protocol-Version": "2.0.0",
}
kafka_post_ingestion_wait_sec = 60


@pytest.fixture(scope="session")
def wait_for_healthchecks():
    # Simply assert that everything is healthy, but don't wait.
    assert not check_local_docker_containers()
    yield


@pytest.mark.dependency()
def test_healthchecks(wait_for_healthchecks):
    # Call to wait_for_healthchecks fixture will do the actual functionality.
    pass

@pytest.fixture(scope="session")
def frontend_session(wait_for_healthchecks):
    session = requests.Session()

    headers = {
        "Content-Type": "application/json",
    }
    data = '{"username":"datahub", "password":"datahub"}'
    response = session.post(
        f"{FRONTEND_ENDPOINT}/logIn", headers=headers, data=data
    )
    response.raise_for_status()

    yield session

@pytest.mark.dependency(depends=["test_healthchecks"])
def test_ingestion_via_rest(wait_for_healthchecks):
    ingest_file_via_rest(bootstrap_sample_data)


@pytest.mark.dependency(depends=["test_healthchecks"])
def test_ingestion_usage_via_rest(wait_for_healthchecks):
    ingest_file_via_rest(usage_sample_data)


@pytest.mark.dependency(depends=["test_healthchecks"])
def test_ingestion_via_kafka(wait_for_healthchecks):
    pipeline = Pipeline.create(
        {
            "source": {
                "type": "file",
                "config": {"filename": bq_sample_data},
            },
            "sink": {
                "type": "datahub-kafka",
                "config": {
                    "connection": {
                        "bootstrap": KAFKA_BROKER,
                    }
                },
            },
        }
    )
    pipeline.run()
    pipeline.raise_from_status()

    # Since Kafka emission is asynchronous, we must wait a little bit so that
    # the changes are actually processed.
    time.sleep(kafka_post_ingestion_wait_sec)


@pytest.mark.dependency(
    depends=[
        "test_ingestion_via_rest",
        "test_ingestion_via_kafka",
        "test_ingestion_usage_via_rest",
    ]
)
def test_run_ingestion(wait_for_healthchecks):
    # Dummy test so that future ones can just depend on this one.
    pass


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_gms_get_user():
    username = "jdoe"
    urn = f"urn:li:corpuser:{username}"
    response = requests.get(
        f"{GMS_ENDPOINT}/entities/{urllib.parse.quote(urn)}",
        headers={
            **restli_default_headers,
        },
    )
    response.raise_for_status()
    data = response.json()

    assert data["value"]
    assert data["value"]["com.linkedin.metadata.snapshot.CorpUserSnapshot"]
    assert data["value"]["com.linkedin.metadata.snapshot.CorpUserSnapshot"]["urn"] == urn


@pytest.mark.parametrize(
    "platform,dataset_name,env",
    [
        (
            # This one tests the bootstrap sample data.
            "urn:li:dataPlatform:kafka",
            "SampleKafkaDataset",
            "PROD",
        ),
        (
            # This one tests BigQuery ingestion.
            "urn:li:dataPlatform:bigquery",
            "bigquery-public-data.covid19_geotab_mobility_impact.us_border_wait_times",
            "PROD",
        ),
    ],
)
@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_gms_get_dataset(platform, dataset_name, env):
    platform = "urn:li:dataPlatform:bigquery"
    dataset_name = (
        "bigquery-public-data.covid19_geotab_mobility_impact.us_border_wait_times"
    )
    env = "PROD"
    urn = f"urn:li:dataset:({platform},{dataset_name},{env})"

    response = requests.get(
        f"{GMS_ENDPOINT}/entities/{urllib.parse.quote(urn)}",
        headers={
            **restli_default_headers,
            "X-RestLi-Method": "get",
        },
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data["value"]
    assert res_data["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
    assert res_data["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]["urn"] == urn

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_gms_batch_get_v2():
    platform = "urn:li:dataPlatform:bigquery"
    env = "PROD"
    name_1 = (
        "bigquery-public-data.covid19_geotab_mobility_impact.us_border_wait_times"
    )
    name_2 = (
        "bigquery-public-data.covid19_geotab_mobility_impact.ca_border_wait_times"
    )
    urn1 = f"urn:li:dataset:({platform},{name_1},{env})"
    urn2 = f"urn:li:dataset:({platform},{name_2},{env})"

    response = requests.get(
        f"{GMS_ENDPOINT}/entitiesV2?ids=List({urllib.parse.quote(urn1)},{urllib.parse.quote(urn2)})&aspects=List(datasetProperties,ownership)",
        headers={
            **restli_default_headers,
            "X-RestLi-Method": "batch_get",
        },
    )
    response.raise_for_status()
    res_data = response.json()

    # Verify both urns exist and have correct aspects
    assert res_data["results"]
    assert res_data["results"][urn1]
    assert res_data["results"][urn1]["aspects"]["datasetProperties"]
    assert res_data["results"][urn1]["aspects"]["ownership"]
    assert res_data["results"][urn2]
    assert res_data["results"][urn2]["aspects"]["datasetProperties"]
    assert "ownership" not in res_data["results"][urn2]["aspects"] # Aspect does not exist.

@pytest.mark.parametrize(
    "query,min_expected_results",
    [
        ("covid", 1),
        ("sample", 3),
    ],
)
@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_gms_search_dataset(query, min_expected_results):


    json = {
            "input": f"{query}",
            "entity": "dataset",
            "start": 0,
            "count": 10
    }
    print(json)
    response = requests.post(
        f"{GMS_ENDPOINT}/entities?action=search",
        headers=restli_default_headers,
        json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data["value"]
    assert res_data["value"]["numEntities"] >= min_expected_results
    assert len(res_data["value"]["entities"]) >= min_expected_results


@pytest.mark.parametrize(
    "query,min_expected_results",
    [
        ("covid", 1),
        ("sample", 3),
    ],
)
@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_gms_search_across_entities(query, min_expected_results):

    json = {
            "input": f"{query}",
            "entities": [],
            "start": 0,
            "count": 10
    }
    print(json)
    response = requests.post(
        f"{GMS_ENDPOINT}/entities?action=searchAcrossEntities",
        headers=restli_default_headers,
        json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data["value"]
    assert res_data["value"]["numEntities"] >= min_expected_results
    assert len(res_data["value"]["entities"]) >= min_expected_results


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_gms_usage_fetch():
    response = requests.post(
        f"{GMS_ENDPOINT}/usageStats?action=queryRange",
        headers=restli_default_headers,
        json={
            "resource": "urn:li:dataset:(urn:li:dataPlatform:bigquery,harshal-playground-306419.test_schema.excess_deaths_derived,PROD)",
            "duration": "DAY",
            "rangeFromEnd": "ALL",
        },
    )
    response.raise_for_status()

    data = response.json()["value"]

    assert len(data["buckets"]) == 6
    assert data["buckets"][0]["metrics"]["topSqlQueries"]

    fields = data["aggregations"].pop("fields")
    assert len(fields) == 12
    assert fields[0]["count"] == 7

    users = data["aggregations"].pop("users")
    assert len(users) == 1
    assert users[0]["count"] == 7

    assert data["aggregations"] == {
        # "fields" and "users" already popped out
        "totalSqlQueries": 7,
        "uniqueUserCount": 1,
    }

@pytest.mark.dependency(depends=["test_healthchecks"])
def test_frontend_auth(frontend_session):
    pass


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_browse_datasets(frontend_session):

    json = {
        "query": """query browse($input: BrowseInput!) {\n
                        browse(input: $input) {\n
                            start\n
                            count\n
                            total\n
                            groups {
                                name
                            }
                            entities {\n
                                ... on Dataset {\n
                                    urn\n
                                    name\n
                                }\n
                            }\n
                        }\n
                    }""",
        "variables": {
            "input": {
                "type": "DATASET",
                "path": ["prod"]
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )

    response.raise_for_status()
    res_data = response.json()
    assert res_data
    assert res_data["data"]
    assert res_data["data"]["browse"]
    assert len(res_data["data"]["browse"]["entities"]) == 0
    assert len(res_data["data"]["browse"]["groups"]) > 0


@pytest.mark.parametrize(
    "query,min_expected_results",
    [
        ("covid", 1),
        ("sample", 3),
        ("", 1),
    ],
)
@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_search_datasets(frontend_session, query, min_expected_results):

    json = {
        "query": """query search($input: SearchInput!) {\n
            search(input: $input) {\n
                start\n
                count\n
                total\n 
                searchResults {\n
                    entity {\n
                        ... on Dataset {\n
                            urn\n
                            name\n
                        }\n
                    }\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
                "type": "DATASET",
                "query": f"{query}",
                "start": 0,
                "count": 10
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["search"]
    assert res_data["data"]["search"]["total"] >= min_expected_results
    assert len(res_data["data"]["search"]["searchResults"]) >= min_expected_results


@pytest.mark.parametrize(
    "query,min_expected_results",
    [
        ("covid", 1),
        ("sample", 3),
        ("", 1),
    ],
)
@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_search_across_entities(frontend_session, query, min_expected_results):

    json = {
        "query": """query searchAcrossEntities($input: SearchAcrossEntitiesInput!) {\n
            searchAcrossEntities(input: $input) {\n
                start\n
                count\n
                total\n 
                searchResults {\n
                    entity {\n
                        ... on Dataset {\n
                            urn\n
                            name\n
                        }\n
                    }\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
                "types": [],
                "query": f"{query}",
                "start": 0,
                "count": 10
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["searchAcrossEntities"]
    assert res_data["data"]["searchAcrossEntities"]["total"] >= min_expected_results
    assert len(res_data["data"]["searchAcrossEntities"]["searchResults"]) >= min_expected_results


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_user_info(frontend_session):

    urn = f"urn:li:corpuser:datahub"
    json = {
        "query": """query corpUser($urn: String!) {\n
            corpUser(urn: $urn) {\n
                urn\n
                username\n
                editableInfo {\n
                    pictureLink\n
                }\n
                info {\n
                    firstName\n
                    fullName\n
                    title\n
                    email\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": urn
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data 
    assert res_data["data"]
    assert res_data["data"]["corpUser"]
    assert res_data["data"]["corpUser"]["urn"] == urn


@pytest.mark.parametrize(
    "platform,dataset_name,env",
    [
        (
            # This one tests the bootstrap sample data.
            "urn:li:dataPlatform:kafka",
            "SampleKafkaDataset",
            "PROD",
        ),
        (
            # This one tests BigQuery ingestion.
            "urn:li:dataPlatform:bigquery",
            "bigquery-public-data.covid19_geotab_mobility_impact.us_border_wait_times",
            "PROD",
        ),
    ],
)
@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_datasets(frontend_session, platform, dataset_name, env):
    urn = f"urn:li:dataset:({platform},{dataset_name},{env})"
    json = {
        "query": """query getDataset($urn: String!) {\n
            dataset(urn: $urn) {\n
                urn\n
                name\n
                description\n
                platform {\n
                    urn\n
                }\n
                schemaMetadata {\n
                    name\n
                    version\n
                    createdAt\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": urn
        }
    }
    # Basic dataset info.
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["dataset"]
    assert res_data["data"]["dataset"]["urn"] == urn
    assert res_data["data"]["dataset"]["name"] == dataset_name
    assert res_data["data"]["dataset"]["platform"]["urn"] == platform

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_ingest_with_system_metadata():
    response = requests.post(
        f"{GMS_ENDPOINT}/entities?action=ingest",
        headers=restli_default_headers,
        json={
          'entity':
            {
              'value':
                {'com.linkedin.metadata.snapshot.CorpUserSnapshot':
                  {'urn': 'urn:li:corpuser:datahub', 'aspects':
                    [{'com.linkedin.identity.CorpUserInfo': {'active': True, 'displayName': 'Data Hub', 'email': 'datahub@linkedin.com', 'title': 'CEO', 'fullName': 'Data Hub'}}]
                   }
                  }
                },
              'systemMetadata': {'lastObserved': 1628097379571, 'runId': 'af0fe6e4-f547-11eb-81b2-acde48001122'}
        },
    )
    response.raise_for_status()

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_ingest_with_blank_system_metadata():
    response = requests.post(
        f"{GMS_ENDPOINT}/entities?action=ingest",
        headers=restli_default_headers,
        json={
          'entity':
            {
              'value':
                {'com.linkedin.metadata.snapshot.CorpUserSnapshot':
                  {'urn': 'urn:li:corpuser:datahub', 'aspects':
                    [{'com.linkedin.identity.CorpUserInfo': {'active': True, 'displayName': 'Data Hub', 'email': 'datahub@linkedin.com', 'title': 'CEO', 'fullName': 'Data Hub'}}]
                   }
                  }
                },
              'systemMetadata': {}
        },
    )
    response.raise_for_status()

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_ingest_without_system_metadata():
    response = requests.post(
        f"{GMS_ENDPOINT}/entities?action=ingest",
        headers=restli_default_headers,
        json={
          'entity':
            {
              'value':
                {'com.linkedin.metadata.snapshot.CorpUserSnapshot':
                  {'urn': 'urn:li:corpuser:datahub', 'aspects':
                    [{'com.linkedin.identity.CorpUserInfo': {'active': True, 'displayName': 'Data Hub', 'email': 'datahub@linkedin.com', 'title': 'CEO', 'fullName': 'Data Hub'}}]
                   }
                  }
             },
        },
    )
    response.raise_for_status()


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_list_policies(frontend_session):

    json = {
        "query": """query listPolicies($input: ListPoliciesInput!) {\n
            listPolicies(input: $input) {\n
                start\n
                count\n
                total\n
                policies {\n
                    urn\n
                    type\n
                    name\n
                    description\n
                    state\n
                    resources {\n
                      type\n
                      allResources\n
                      resources\n
                    }\n
                    privileges\n
                    actors {\n
                      users\n
                      groups\n
                      allUsers\n
                      allGroups\n
                      resourceOwners\n
                    }\n
                    editable\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
              "start": "0",
              "count": "20",
            }
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["listPolicies"]
    assert res_data["data"]["listPolicies"]["start"] is 0
    assert res_data["data"]["listPolicies"]["count"] > 0
    assert len(res_data["data"]["listPolicies"]["policies"]) > 0

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_frontend_list_policies"])
def test_frontend_update_policy(frontend_session):

    json = {
        "query": """mutation updatePolicy($urn: String!, $input: PolicyUpdateInput!) {\n
            updatePolicy(urn: $urn, input: $input) }""",
        "variables": {
            "urn": "urn:li:dataHubPolicy:7",
            "input": {
                "type": "PLATFORM",
                "state": "INACTIVE",
                "name": "Updated Platform Policy",
                "description": "My Metadaata Policy",
                "privileges": [ "MANAGE_POLICIES" ],
                "actors": {
                  "users": ["urn:li:corpuser:datahub"],
                  "resourceOwners": False,
                  "allUsers": False,
                  "allGroups": False
                }
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["updatePolicy"]
    assert res_data["data"]["updatePolicy"] == "urn:li:dataHubPolicy:7"

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_frontend_list_policies", "test_frontend_update_policy"])
def test_frontend_delete_policy(frontend_session):

    json = {
        "query": """mutation deletePolicy($urn: String!) {\n
            deletePolicy(urn: $urn) }""",
        "variables": {
            "urn": "urn:li:dataHubPolicy:7"
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    # Now verify the policy has been removed.
    json = {
        "query": """query listPolicies($input: ListPoliciesInput!) {\n
            listPolicies(input: $input) {\n
                start\n
                count\n
                total\n
                policies {\n
                    urn\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
              "start": "0",
              "count": "20",
            }
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["listPolicies"]
    assert len(res_data["data"]["listPolicies"]["policies"]) is 7 # Length of default policies - 1

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_frontend_list_policies", "test_frontend_delete_policy"])
def test_frontend_create_policy(frontend_session):

    # Policy tests are not idempotent. If you rerun this test it will be wrong.
    json = {
        "query": """mutation createPolicy($input: PolicyUpdateInput!) {\n
            createPolicy(input: $input) }""",
        "variables": {
            "input": {
                "type": "METADATA",
                "name": "Test Metadata Policy",
                "description": "My Metadaata Policy",
                "state": "ACTIVE",
                "resources": {
                  "type": "dataset",
                  "allResources": True
                },
                "privileges": [ "EDIT_ENTITY_TAGS" ],
                "actors": {
                  "users": ["urn:li:corpuser:datahub"],
                  "resourceOwners": False,
                  "allUsers": False,
                  "allGroups": False
                }
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["createPolicy"]

    # Now verify the policy has been added.
    json = {
        "query": """query listPolicies($input: ListPoliciesInput!) {\n
            listPolicies(input: $input) {\n
                start\n
                count\n
                total\n
                policies {\n
                    urn\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
              "start": "0",
              "count": "20",
            }
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["listPolicies"]
    # TODO: Check to see that the policy we created in inside the array.
    assert len(res_data["data"]["listPolicies"]["policies"]) is 8 # Back up to 8

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_app_config(frontend_session):

    json = {
        "query": """query appConfig {\n
            appConfig {\n
                analyticsConfig {\n
                  enabled\n
                }\n
                policiesConfig {\n
                  enabled\n
                  platformPrivileges {\n
                    type\n
                    displayName\n
                    description\n
                  }\n
                  resourcePrivileges {\n
                    resourceType\n
                    resourceTypeDisplayName\n
                    entityType\n
                    privileges {\n
                      type\n
                      displayName\n
                      description\n
                    }\n
                  }\n
                }\n
            }\n
        }"""
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["appConfig"]
    assert res_data["data"]["appConfig"]["analyticsConfig"]["enabled"] is True
    assert res_data["data"]["appConfig"]["policiesConfig"]["enabled"] is True

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_frontend_me_query(frontend_session):

    json = {
        "query": """query me {\n
            me {\n
                corpUser {\n
                  urn\n
                  username\n
                  editableInfo {\n
                      pictureLink\n
                  }\n
                  info {\n
                      firstName\n
                      fullName\n
                      title\n
                      email\n
                  }\n
                }\n
                platformPrivileges {\n
                  viewAnalytics
                  managePolicies
                  manageIdentities
                  generatePersonalAccessTokens
                }\n
            }\n
        }"""
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["me"]["corpUser"]["urn"] == "urn:li:corpuser:datahub"
    assert res_data["data"]["me"]["platformPrivileges"]["viewAnalytics"] is True
    assert res_data["data"]["me"]["platformPrivileges"]["managePolicies"] is True
    assert res_data["data"]["me"]["platformPrivileges"]["manageIdentities"] is True
    assert res_data["data"]["me"]["platformPrivileges"]["generatePersonalAccessTokens"] is True


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_list_users(frontend_session):

    json = {
        "query": """query listUsers($input: ListUsersInput!) {\n
            listUsers(input: $input) {\n
                start\n
                count\n
                total\n
                users {\n
                    urn\n
                    type\n
                    username\n
                    properties {\n
                      firstName
                    }\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
              "start": "0",
              "count": "2",
            }
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["listUsers"]
    assert res_data["data"]["listUsers"]["start"] is 0
    assert res_data["data"]["listUsers"]["count"] is 2
    assert len(res_data["data"]["listUsers"]["users"]) >= 2 # Length of default user set.

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_list_groups(frontend_session):

    json = {
        "query": """query listGroups($input: ListGroupsInput!) {\n
            listGroups(input: $input) {\n
                start\n
                count\n
                total\n
                groups {\n
                    urn\n
                    type\n
                    name\n
                    properties {\n
                      displayName
                    }\n
                }\n
            }\n
        }""",
        "variables": {
            "input": {
              "start": "0",
              "count": "2",
            }
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["listGroups"]
    assert res_data["data"]["listGroups"]["start"] is 0
    assert res_data["data"]["listGroups"]["count"] is 2
    assert len(res_data["data"]["listGroups"]["groups"]) >= 2 # Length of default group set.

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_list_groups"])
def test_add_remove_members_from_group(frontend_session):

    # Assert no group edges for user jdoe
    json = {
        "query": """query corpUser($urn: String!) {\n
            corpUser(urn: $urn) {\n
                urn\n
                relationships(input: { types: ["IsMemberOfGroup"], direction: OUTGOING, start: 0, count: 1 }) {\n
                    total\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": "urn:li:corpuser:jdoe"
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["corpUser"]
    assert res_data["data"]["corpUser"]["relationships"]["total"] is 0

    # Add jdoe to group
    json = {
        "query": """mutation addGroupMembers($input: AddGroupMembersInput!) {\n
            addGroupMembers(input: $input) }""",
        "variables": {
            "input": {
                "groupUrn": "urn:li:corpGroup:bfoo",
                "userUrns": [ "urn:li:corpuser:jdoe" ],
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()

    # Sleep for edge store to be updated. Not ideal!
    time.sleep(1)

    # Verify the member has been added
    json = {
        "query": """query corpUser($urn: String!) {\n
            corpUser(urn: $urn) {\n
                urn\n
                relationships(input: { types: ["IsMemberOfGroup"], direction: OUTGOING, start: 0, count: 1 }) {\n
                    total\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": "urn:li:corpuser:jdoe"
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["corpUser"]
    assert res_data["data"]["corpUser"]["relationships"]
    assert res_data["data"]["corpUser"]["relationships"]["total"] is 1

    # Now remove jdoe from the group
    json = {
        "query": """mutation removeGroupMembers($input: RemoveGroupMembersInput!) {\n
            removeGroupMembers(input: $input) }""",
        "variables": {
            "input": {
                "groupUrn": "urn:li:corpGroup:bfoo",
                "userUrns": [ "urn:li:corpuser:jdoe" ],
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()

    # Sleep for edge store to be updated. Not ideal!
    time.sleep(1)

    # Verify the member has been removed
    json = {
        "query": """query corpUser($urn: String!) {\n
            corpUser(urn: $urn) {\n
                urn\n
                relationships(input: { types: ["IsMemberOfGroup"], direction: OUTGOING, start: 0, count: 1 }) {\n
                    total\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": "urn:li:corpuser:jdoe"
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["corpUser"]
    assert res_data["data"]["corpUser"]["relationships"]["total"] is 0

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_list_groups", "test_add_remove_members_from_group"])
def test_remove_user(frontend_session):

    json = {
        "query": """mutation removeUser($urn: String!) {\n
            removeUser(urn: $urn) }""",
        "variables": {
            "urn": "urn:li:corpuser:jdoe"
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()

    json = {
        "query": """query corpUser($urn: String!) {\n
            corpUser(urn: $urn) {\n
                urn\n
                properties {\n
                    firstName\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": "urn:li:corpuser:jdoe"
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["corpUser"]
    assert res_data["data"]["corpUser"]["properties"] is None

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_list_groups", "test_add_remove_members_from_group"])
def test_remove_group(frontend_session):

    json = {
        "query": """mutation removeGroup($urn: String!) {\n
            removeGroup(urn: $urn) }""",
        "variables": {
          "urn": "urn:li:corpGroup:bfoo"
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()

    json = {
        "query": """query corpGroup($urn: String!) {\n
            corpGroup(urn: $urn) {\n
                urn\n
                properties {\n
                    displayName\n
                }\n
            }\n
        }""",
        "variables": {
            "urn": "urn:li:corpGroup:bfoo"
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["corpGroup"]
    assert res_data["data"]["corpGroup"]["properties"] is None

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion", "test_list_groups", "test_remove_group"])
def test_create_group(frontend_session):

    json = {
        "query": """mutation createGroup($input: CreateGroupInput!) {\n
            createGroup(input: $input) }""",
        "variables": {
            "input": {
                "name": "Test Group",
                "description": "My test group"
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()

    json = {
        "query": """query corpGroup($urn: String!) {\n
            corpGroup(urn: $urn) {\n
                urn\n
                properties {\n
                    displayName\n
                }\n
            }\n
        }""",
        "variables": {
          "urn": "urn:li:corpGroup:Test Group"
        }
    }
    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["corpGroup"]
    assert res_data["data"]["corpGroup"]["properties"]["displayName"] == "Test Group"

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_home_page_recommendations(frontend_session):

    min_expected_recommendation_modules = 0

    json = {
        "query": """query listRecommendations($input: ListRecommendationsInput!) {\n
            listRecommendations(input: $input) { modules { title } } }""",
        "variables": {
            "input": {
                "userUrn": "urn:li:corpuser:datahub",
                "requestContext": {
                  "scenario": "HOME"
                },
                "limit": 5
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()
    print(res_data)

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["listRecommendations"]
    assert "error" not in res_data
    assert len(res_data["data"]["listRecommendations"]["modules"]) > min_expected_recommendation_modules

@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_search_results_recommendations(frontend_session):

    # This test simply ensures that the recommendations endpoint does not return an error.
    json = {
        "query": """query listRecommendations($input: ListRecommendationsInput!) {\n
            listRecommendations(input: $input) { modules { title }  }""",
        "variables": {
            "input": {
                "userUrn": "urn:li:corpuser:datahub",
                "requestContext": {
                  "scenario": "SEARCH_RESULTS",
                  "searchRequestContext": {
                    "query": "asdsdsdds",
                    "filters": []
                  }
                },
                "limit": 5
            }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert "error" not in res_data


@pytest.mark.dependency(depends=["test_healthchecks", "test_run_ingestion"])
def test_generate_personal_access_token(frontend_session):

    # Test success case
    json = {
        "query": """query getAccessToken($input: GetAccessTokenInput!) {\n
            getAccessToken(input: $input) {\n
              accessToken\n
            }\n
        }""",
        "variables": {
          "input": {
              "type": "PERSONAL",
              "actorUrn": "urn:li:corpuser:datahub",
              "duration": "ONE_MONTH"
          }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert res_data["data"]
    assert res_data["data"]["getAccessToken"]["accessToken"] is not None
    assert "error" not in res_data

    # Test unauthenticated case
    json = {
        "query": """query getAccessToken($input: GetAccessTokenInput!) {\n
            accessToken\n
        }""",
        "variables": {
          "input": {
              "type": "PERSONAL",
              "actorUrn": "urn:li:corpuser:jsmith",
              "duration": "ONE_DAY"
          }
        }
    }

    response = frontend_session.post(
        f"{FRONTEND_ENDPOINT}/api/v2/graphql", json=json
    )
    response.raise_for_status()
    res_data = response.json()

    assert res_data
    assert "errors" in res_data # Assert the request fails


@pytest.mark.dependency(depends=["test_healthchecks"])
def test_stateful_ingestion(wait_for_healthchecks):
    def create_mysql_engine(mysql_source_config_dict: Dict[str, Any]) -> Any:
        mysql_config = MySQLConfig.parse_obj(mysql_source_config_dict)
        url = mysql_config.get_sql_alchemy_url()
        return create_engine(url)

    def create_table(engine: Any, name:str, defn:str) -> None:
        create_table_query = text(f"CREATE TABLE IF NOT EXISTS {name}{defn};")
        engine.execute(create_table_query)

    def drop_table(engine: Any, table_name: str) -> None:
        drop_table_query = text(f"DROP TABLE {table_name};")
        engine.execute(drop_table_query)

    def get_current_checkpoint_from_pipeline(
            pipeline_config_dict: Dict[str, Any]
    ) -> Optional[Checkpoint]:
        pipeline = Pipeline.create(pipeline_config_dict)
        pipeline.run()
        pipeline.raise_from_status()
        mysql_source = cast(MySQLSource, pipeline.source)
        return mysql_source.get_current_checkpoint(
            mysql_source.get_default_ingestion_job_id()
        )

    source_config_dict: Dict[str, Any] = {
            "username": "datahub",
            "password": "datahub",
            "database": "datahub",
            "stateful_ingestion": {
                "enabled": True,
                "remove_stale_metadata": True,
                "state_provider": {
                    "type": "datahub",
                    "config": {"datahub_api": {"server": "http://localhost:8080"}},
                },
            },
        }

    pipeline_config_dict: Dict[str, Any] = {
            "source": {
                "type": "mysql",
                "config": source_config_dict,
            },
            "sink": {
                "type": "datahub-rest",
                "config": {"server": "http://localhost:8080"},
            },
            "pipeline_name": "mysql_stateful_ingestion_smoke_test_pipeline",
        }

    # 1. Setup the SQL engine
    mysql_engine = create_mysql_engine(source_config_dict)

    # 2. Create test tables for first run of the  pipeline.
    table_prefix = "stateful_ingestion_test"
    table_defs = {
        f"{table_prefix}_t1": "(id INT, name VARCHAR(10))",
        f"{table_prefix}_t2": "(id INT)"
    }
    table_names = sorted(table_defs.keys())
    for table_name, defn in table_defs.items():
        create_table(mysql_engine, table_name, defn)

    # 3. Do the first run of the pipeline and get the default job's checkpoint.
    checkpoint1 = get_current_checkpoint_from_pipeline(
        pipeline_config_dict=pipeline_config_dict
    )
    assert checkpoint1

    # 4. Drop table t1 created during step 2 + rerun the pipeline and get the checkpoint state.
    drop_table(mysql_engine, table_names[0])
    checkpoint2 = get_current_checkpoint_from_pipeline(
        pipeline_config_dict=pipeline_config_dict
    )
    assert checkpoint2

    # 5. Perform all assertions on the states
    state1 = cast(BaseSQLAlchemyCheckpointState, checkpoint1.state)
    state2 = cast(BaseSQLAlchemyCheckpointState, checkpoint2.state)
    difference_urns = list(state1.get_table_urns_not_in(state2))
    assert len(difference_urns) == 1
    assert (
        difference_urns[0]
        == "urn:li:dataset:(urn:li:dataPlatform:mysql,datahub.stateful_ingestion_test_t1,PROD)"
    )

    # 6. Perform all assertions on the config.
    assert checkpoint1.config == checkpoint2.config

    # 7. Cleanup table t2 as well to prevent other tests that rely on data in the smoke-test world.
    drop_table(mysql_engine, table_names[1])
