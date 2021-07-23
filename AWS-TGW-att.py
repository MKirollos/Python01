"""
Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

AWS Disclaimer.

(c) 2019 Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
This AWS Content is provided subject to the terms of the AWS Customer
Agreement available at https://aws.amazon.com/agreement/ or other written
agreement between Customer and Amazon Web Services, Inc.

Looks up VPC and associated subnets based on tags.
Returns the VPC and Subnet values back to the custom resource.

Runtime: python3.6
Last Modified: 2/6/2019
"""

"""
Updated on 25/03/21. Changes:
* CIDR is now passed as a comma separated string. This is then casted to a LIST
* Updated the 'create_vpc_route_to_tgw' method to add a route for each block in our CIDR list
* Updated the 'create_vpc_route_to_tgw' method to add routes in each routing table based on a tag

"""

import boto3
import json
import logging
import urllib3
import os
import time

EC2_CLIENT = boto3.client('ec2')
IAM_CLIENT = boto3.client('iam')

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def lambda_handler(event, context):
    response_data = {}
    setup_logging()
    log.info('In Main Handler')
    log.info(json.dumps(event))
    print(json.dumps(event))

    account = event['ResourceProperties']['Account']
    region = event['ResourceProperties']['Region']
    vpc_tags = event['ResourceProperties']['Vpc_Tags']
    # CIDR has now been changed to a list to allow us to pass in a string of CIDrs that we care about.
    # Subsequent calls that use CIDR will use this LIST and loop through each item in teh list
    cidr = (event['ResourceProperties']['CIDR']).split(
        ",")  # CIDRs will be passed from the CFN as x.x.x.x/32,x.x.x.x/21
    tgw_id = event['ResourceProperties']['Transit_Gateway_Id']

    if event['RequestType'] in ['Update', 'Create']:
        log.info('Event = ' + event['RequestType'])

        create_service_link_role()
        vpc_metadata = get_vpc_metadata(account, region, vpc_tags, cidr)
        create_transit_gateways(vpc_metadata, tgw_id)
        create_vpc_route_to_tgw(vpc_metadata, tgw_id, cidr)

        send(event, context, 'SUCCESS', response_data)

    else:
        log.error("failed to run")
        send(event, context, 'FAILED', response_data)

    if event['RequestType'] in ['Delete']:
        log.info('Event = ' + event['RequestType'])

        send(event, context, 'SUCCESS', response_data)


def create_vpc_route_to_tgw(vpc_metadata, tgw_id, cidr: list):
    response_data = {}

    for entry in vpc_metadata:
        if entry['Subnet']:
            try:
                describe_routes = EC2_CLIENT.describe_route_tables(
                    RouteTableIds=entry['Route_Tables'],
                )

                describe_routes = describe_routes['RouteTables']

                for rt in describe_routes:
                    for route in rt['Routes']:
                        if "DestinationCidrBlock" in route:
                            if route['DestinationCidrBlock'] == cidr:
                                EC2_CLIENT.delete_route(
                                    DestinationCidrBlock=cidr,
                                    RouteTableId=rt["RouteTableId"]
                                )
                    for block in cidr:
                        EC2_CLIENT.create_route(
                            RouteTableId=rt["RouteTableId"],
                            DestinationCidrBlock=block,
                            TransitGatewayId=tgw_id
                        )
                        log.error('CREATED ROUTE to ' + block + ' for ' + rt["RouteTableId"] +
                                  ' with a destination of ' + tgw_id)

            except Exception as e:
                log.error(e)
                return None


def create_transit_gateways(vpc_metadata, tgw_id):
    for entry in vpc_metadata:
        if entry['Subnet']:
            try:
                response = EC2_CLIENT.create_transit_gateway_vpc_attachment(
                    TransitGatewayId=tgw_id,
                    VpcId=entry['Vpc'],
                    SubnetIds=entry['Subnet'],
                )

            except Exception as e:
                log.error(e)
                return None
        else:
            print('No subnets in VPC,' + entry['Vpc'] + ' unable to attach VPC')

    time.sleep(90)


def get_vpc_metadata(account, region, vpc_tags, cidr):
    vpc_tags = vpc_tags.replace(' ', '')
    vpc_tags = vpc_tags.split(',')

    returned_metadata = []

    for tag in vpc_tags:
        try:
            get_vpc_response = EC2_CLIENT.describe_vpcs()
            for vpc in get_vpc_response['Vpcs']:
                if 'Tags' in vpc:
                    for tag_value in vpc['Tags']:
                        if tag_value['Value'] == tag:
                            metadata = {}
                            returned_vpc = vpc['VpcId']
                            subnets = get_subnets(returned_vpc)
                            route_tables = get_default_route_table(returned_vpc, cidr, vpc_tag="True")
                            metadata['Vpc'] = returned_vpc
                            metadata['Subnet'] = subnets
                            metadata['Route_Tables'] = route_tables
                            returned_metadata.append(metadata)

        except Exception as e:
            log.error(e)
            return None

    return returned_metadata


def get_subnets(returned_vpc, vpc_tag="True"):
    subnet_list = []
    az_subnet_mapping = []

    try:
        get_subnet_response = EC2_CLIENT.describe_subnets(
            Filters=[
                {
                    'Name': 'vpc-id',
                    'Values': [returned_vpc]
                },
                {
                    'Name': 'tag:tgw-attach',
                    'Values': [vpc_tag]
                }
            ])

        for entry in get_subnet_response['Subnets']:
            subnet_list.append(entry['SubnetId'])

        for subnet in subnet_list:
            response = EC2_CLIENT.describe_subnets(
                Filters=[
                    {
                        'Name': 'subnet-id',
                        'Values': [subnet]
                    },
                ],
            )

            for sub in response['Subnets']:
                if not any(sub['AvailabilityZone'] in az for az in az_subnet_mapping):
                    az_subnet_mapping.append({
                        sub['AvailabilityZone']: sub['SubnetId']
                    })

    except Exception as e:
        log.error(e)
        return None

    subnets = []

    for subnet_mapping in az_subnet_mapping:
        for key, value in subnet_mapping.items():
            subnets.append(value)

    return subnets


def get_default_route_table(returned_vpc, cidr, vpc_tag="True"):
    try:
        describe_route_tables = EC2_CLIENT.describe_route_tables(
            Filters=[
                {
                    'Name': 'vpc-id',
                    'Values': [returned_vpc]
                },
                {
                    'Name': 'association.main',
                    'Values': ['false']

                },
                {
                    'Name': 'tag:tgw-attach',
                    'Values': [vpc_tag]
                }

            ]
        )

        route_table_ids = [rt['RouteTableId'] for rt in describe_route_tables['RouteTables']]

        for route_table in route_table_ids:
            describe_routes = EC2_CLIENT.describe_route_tables(
                RouteTableIds=[
                    route_table,
                ],
            )
            describe_routes = describe_routes['RouteTables']

            for route in describe_routes[0]['Routes']:
                if "DestinationCidrBlock" in route:
                    for block in cidr:
                        if route['DestinationCidrBlock'] == cidr:
                            EC2_CLIENT.delete_route(
                                DestinationCidrBlock=block,
                                RouteTableId=route_table
                            )

    except Exception as e:
        log.error(e)
        return None

    return route_table_ids


def create_service_link_role():
    service_role_exists = False

    list_roles = IAM_CLIENT.list_roles(
    )

    for role in list_roles['Roles']:
        if role['RoleName'] == 'AWSServiceRoleForVPCTransitGateway':
            service_role_exists = True

    if not service_role_exists:
        create_role = IAM_CLIENT.create_service_linked_role(
            AWSServiceName='transitgateway.amazonaws.com',
        )
        print(create_role)

    return ()


def setup_logging():
    """Setup Logging."""
    global log
    log = logging.getLogger()
    log_levels = {'INFO': 20, 'WARNING': 30, 'ERROR': 40}

    if 'logging_level' in os.environ:
        log_level = os.environ['logging_level'].upper()
        if log_level in log_levels:
            log.setLevel(log_levels[log_level])
        else:
            log.setLevel(log_levels['ERROR'])
            log.error("The logging_level environment variable is not set \
                      to INFO, WARNING, or ERROR. \
                      The log level is set to ERROR")
    else:
        log.setLevel(log_levels['ERROR'])
        log.warning('The logging_level environment variable is not set.')
        log.warning('Setting the log level to ERROR')
    log.info('Logging setup complete - set to log level '
             + str(log.getEffectiveLevel()))


def send(event, context, responseStatus, response_data, physicalResourceId=None, noEcho=False):
    responseUrl = event['ResponseURL']

    print(responseUrl)

    responseBody = {}
    responseBody['Status'] = responseStatus
    responseBody['Reason'] = 'See the details in CloudWatch Log Stream: ' + \
                             context.log_stream_name
    responseBody['PhysicalResourceId'] = physicalResourceId or context.log_stream_name
    responseBody['StackId'] = event['StackId']
    responseBody['RequestId'] = event['RequestId']
    responseBody['LogicalResourceId'] = event['LogicalResourceId']
    responseBody['NoEcho'] = noEcho
    responseBody['Data'] = response_data

    json_responseBody = json.dumps(responseBody)

    print("Response body:\n" + json_responseBody)

    headers = {
        'content-type': '',
        'content-length': str(len(json_responseBody))
    }

    try:
        http = urllib3.PoolManager()
        response = http.request("PUT", responseUrl, headers=headers, body=json_responseBody)
        print("Status code:" + response.reason)

    except Exception as e:
        print("send(..) failed executing requests.put(..): " + str(e))
