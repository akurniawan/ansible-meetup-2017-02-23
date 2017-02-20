#!/usr/bin/env python
from functools import wraps
import re
import syslog
import time

import botocore
import boto.vpc
import boto.ec2
import boto.ec2.autoscale
import boto3

from ansible import errors


class CloudRetry(object):
    """ CloudRetry can be used by any cloud provider, in order to implement a
        backoff algorithm/retry effect based on Status Code from Exceptions.
    """
    # This is the base class of the exception.
    # AWS Example botocore.exceptions.ClientError
    base_class = None

    @staticmethod
    def status_code_from_exception(error):
        """ Return the status code from the exception object
        Args:
            error (object): The exception itself.
        """
        pass

    @staticmethod
    def found(response_code):
        """ Return True if the Response Code to retry on was found.
        Args:
            response_code (str): This is the Response Code that is being matched against.
        """
        pass

    @classmethod
    def backoff(cls, tries=10, delay=3, backoff=2):
        """ Retry calling the Cloud decorated function using an exponential backoff.
        Kwargs:
            tries (int): Number of times to try (not retry) before giving up
                default=10
            delay (int): Initial delay between retries in seconds
                default=3
            backoff (int): backoff multiplier e.g. value of 2 will double the delay each retry
                default=2

        """
        def deco(f):
            @wraps(f)
            def retry_func(*args, **kwargs):
                max_tries, max_delay = tries, delay
                while max_tries > 1:
                    try:
                        return f(*args, **kwargs)
                    except Exception as e:
                        if isinstance(e, cls.base_class):
                            response_code = cls.status_code_from_exception(e)
                            if cls.found(response_code):
                                msg = "{0}: Retrying in {1} seconds...".format(
                                    str(e), max_delay)
                                syslog.syslog(syslog.LOG_INFO, msg)
                                time.sleep(max_delay)
                                max_tries -= 1
                                max_delay *= backoff
                            else:
                                # Return original exception if exception is not
                                # a ClientError
                                raise e
                        else:
                            # Return original exception if exception is not a
                            # ClientError
                            raise e
                return f(*args, **kwargs)

            return retry_func  # true decorator

        return deco


class AWSRetry(CloudRetry):
    base_class = botocore.exceptions.ClientError

    @staticmethod
    def status_code_from_exception(error):
        return error.response['Error']['Code']

    @staticmethod
    def found(response_code):
        # This list of failures is based on this API Reference
        # http://docs.aws.amazon.com/AWSEC2/latest/APIReference/errors-overview.html
        retry_on = [
            'RequestLimitExceeded', 'Unavailable', 'ServiceUnavailable',
            'InternalFailure', 'InternalError'
        ]

        not_found = re.compile(r'^\w+.NotFound')
        if response_code in retry_on or not_found.search(response_code):
            return True
        else:
            return False


@AWSRetry.backoff()
def aws_client(region, service='ec2', profile=None):
    """ Set the boto3 client with the correct service and AWS profile.

    Args:
        region (str): The AWS region you want this client to connect to.
            example us-west-2
    Kwargs:
        service (str): The service this client will connect to.
        profile (str): The aws profile name that is set in ~/.aws/credentials

    Basic Usage:
        >>> client = aws_client('us-west-2', 'kinesis', profile='prod')

    Returns:
        botocore.client.EC2
    """
    try:
        session = boto3.Session(region_name=region, profile_name=profile)
        return session.client(service)
    except botocore.exceptions.ClientError as e:
        raise e


def get_account_id(region, profile=None):
    """ Retrieve the AWS account id.
    Args:
        region (str): The AWS region.

    Basic Usage:
        >>> region = 'us-west-2'
        >>> account_id = get_account_id(region)
        12345667899

    Returns:
        String
    """
    client = aws_client(region, 'iam', profile)
    try:
        account_id = client.list_users()['Users'][0]['Arn'].split(':')[4]
        return account_id
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                "Failed to retrieve account id"
            )


@AWSRetry.backoff()
def get_sg_cidrs(name, vpc_id, region, profile=None):
    """
    Args:
        name (str): The name of the security group you are looking for.
        vpc_id (str): The VPC id where this security group resides.
        region (str): The AWS region.

    Basic Usage:
        >>> name = 'ProductionELB'
        >>> region = 'us-west-2'
        >>> vpc_id = 'vpc-123456'
        >>> security_group_id = get_sg(name, vpc_id, region)
        >>> print security_group_id

    Returns:
        String
    """
    client = aws_client(region, 'ec2', profile)
    params = {
        "Filters": [
            {
                "Name": "tag-key",
                "Values": ["Name"]
            },
            {
                "Name": "tag-value",
                "Values": [name]
            },
            {
                "Name": "vpc-id",
                "Values": [vpc_id],
            }
        ]
    }
    try:
        sg_groups = client.describe_security_groups(**params)['SecurityGroups']
        if len(sg_groups) == 1:
            cidrs = map(lambda x: x['CidrIp'], sg_groups[0] \
                    ['IpPermissions'][0]['IpRanges'])
            return cidrs
        elif len(sg_groups) > 1:
            raise errors.AnsibleFilterError(
                "Too many results for {0}: {1}".format(
                    name, ",".join(sg_groups)
                )
            )
        else:
            raise errors.AnsibleFilterError(
                "Security Group {0} was not found".format(name)
            )
    except botocore.exceptions.ClientError as e:
        raise e


@AWSRetry.backoff()
def get_sg(name, vpc_id, region=None, profile=None):
    """
    Args:
        name (str): The name of the security group you are looking for.
        vpc_id (str): The VPC id where this security group resides.
        region (str): The AWS region.

    Basic Usage:
        >>> name = 'ProductionELB'
        >>> region = 'us-west-2'
        >>> vpc_id = 'vpc-123456'
        >>> security_group_id = get_sg(name, vpc_id, region)
        >>> print security_group_id

    Returns:
        String
    """

    sg_id = get_sg_ids_by_names([name], vpc_id, region, profile)
    if len(sg_id) > 1:
        raise errors.AnsibleFilterError(
            "Too many results for {0}".format(name)
        )
    return sg_id


@AWSRetry.backoff()
def get_sg_ids_by_names(names, vpc_id, region=None, profile=None):
    """
    Args:
        names (list): The list of names of the security group you are looking for.
        vpc_id (str): The VPC id where this security group resides.
        region (str): The AWS region.

    Basic Usage:
        >>> names = ['ProductionELB', 'ProductionEC2']
        >>> region = 'us-west-2'
        >>> vpc_id = 'vpc-123456'
        >>> security_group_ids = get_sg_ids_by_names(names, vpc_id, region)
        >>> print security_group_ids

    Returns:
        List of Strings
    """
    client = aws_client(region, 'ec2', profile)
    filters = [
        {
            "Name": "vpc-id",
            "Values": [vpc_id],
        },
        {
            "Name": "group-name",
            "Values": names,
        }
    ]
    try:
        sg_groups = client.describe_security_groups(
                Filters=filters)["SecurityGroups"]
        if len(sg_groups) == 0:
            raise errors.AnsibleFilterError(
                "Security group {0} was not found".format(names)
            )
        return [sg_group["GroupId"] for sg_group in sg_groups]
    except botocore.exceptions.ClientError as e:
        raise e


@AWSRetry.backoff()
def get_sgs_by_tags(region, return_key="GroupId", profile=None, **tags):
    """Retrieve list of key from 1 or multiple security groups filtered by 1 or multiple tags.
    Args:
        region (str): The AWS region.
        return_key (str): the property of the security groups you want to return.
            default=GroupId

    Kwargs:
        tags (dict): The tags you want to filter by.

    Basic Usage:
        >>> region = 'us-west-2'
        >>> sg_groups = get_sgs_by_tags(
                region, name='superturbo-webapp', env='foobar')
        ['sg-abcdef123', 'sg-ghijkl456']

    Returns:
        List(string)
    """


    try:
        client = aws_client(region, 'ec2', profile)
        result = list()
        filters = list()
        for key, val in tags.items():
            filters.append(
                {
                    'Name': "tag:{0}".format(key),
                    'Values': [val]
                }
            )

        sg_groups = client.describe_security_groups(
            Filters=filters)["SecurityGroups"]

        if len(sg_groups) > 0:
            for sg_group in sg_groups:
                result.append(sg_group.get(return_key))
            return result
        elif len(sg_groups) == 0:
            raise errors.AnsibleFilterError(
                "No security group was found with tag {0} in region {1}"
                .format(tags, region)
            )
    except Exception as e:
        raise e


@AWSRetry.backoff()
def get_sg_by_tags(region, return_key="GroupId", profile=None, **tags):
    """Retrieve key from 1 security group filtered by 1 or multiple tags.
    Args:
        region (str): The AWS region.
        return_key (str): the property of the security group you want to return.
            default=GroupId

    Kwargs:
        tags (dict): The tags you want to filter by.

    Basic Usage:
        >>> region = 'us-west-2'
        >>> sg_groups = get_sg_by_tags(
                region, name='hyperturbo-webapp', env='foobar')
        'sg-abcdef123'

    Returns:
        String
    """

    try:
        sg_groups = get_sgs_by_tags(region, return_key, profile, **tags)

        if len(sg_groups) == 1:
            result = sg_groups[0]
            return result
        elif len(sg_groups) > 1:
            raise errors.AnsibleFilterError(
                "More than 1 security groups was found with tag {0} in region {1}"
                .format(tags, region)
            )
        elif len(sg_groups) == 0:
            raise errors.AnsibleFilterError(
                "No security group was found with tag {0} in region {1}"
                .format(tags, region)
            )
    except Exception as e:
        raise e


@AWSRetry.backoff()
def get_server_certificate(name, region=None, profile=None):
    """ Retrieve the ARN of a server certificate.
    Args:
        name: (str): The name of the server certificate.

    Kwargs:
        region (str): The AWS region.
        profile (str): The ~/.aws/credentials profile name to use

    Basic Usage:
        >>> arn = get_server_certificate('test', region='us-west-2')
        'arn:aws:iam::12345678910:server-certificate/test'

    Returns:
        String
    """
    client = aws_client(region, 'iam', profile)
    try:
        cert_meta = (
            client.get_server_certificate(
                ServerCertificateName=name
            )['ServerCertificate']['ServerCertificateMetadata']
        )
        return_key = cert_meta['Arn']
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                "Server Certificate {0} was not found".format(name)
            )

    return return_key


@AWSRetry.backoff()
def get_instance_profile(name, region=None, profile=None):
    """ Retrieve the instance profile of an IAM role.
    Args:
        name: (str): The name of the IAM role.

    Kwargs:
        region (str): The AWS region.
        profile (str): The ~/.aws/credentials profile name to use

    Basic Usage:
        >>> arn = get_instance_profile('test', region='us-west-2')
        'arn:aws:iam::12345678910:instance-profile/test'

    Returns:
        String
    """
    client = aws_client(region, 'iam', profile)
    try:
        profile = (
            client.get_instance_profile(
                InstanceProfileName=name
            )['InstanceProfile']
        )
        return_key = profile['Arn']
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                "IAM instance profile {0} was not found".format(name)
            )

    return return_key


@AWSRetry.backoff()
def get_sqs(name, key='arn', region=None, profile=None):
    """ Retrieve the arn or url a SQS Queue.
    Args:
        name: (str): The SQS name.

    Kwargs:
        key (str): The key you want returned from the SQS Queue (url or arn)
            default=arn
        region (str): The AWS region.
        profile (str): The ~/.aws/credentials profile name to use

    Basic Usage:
        >>> arn = get_sqs('test', region='us-west-2')
        'arn:aws:sqs:us-west-2:12345678910:test'

    Returns:
        String
    """
    client = aws_client(region, 'sqs', profile)
    try:
        url = client.get_queue_url(QueueName=name)['QueueUrl']
        if key == 'arn':
            attributes = (
                client.get_queue_attributes(
                    QueueUrl=url, AttributeNames=['QueueArn']
                )['Attributes']
            )
            return_key = attributes['QueueArn']
        else:
            return_key = url
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                "SQS Queue {0} was not found".format(name)
            )

    return return_key


@AWSRetry.backoff()
def get_dynamodb_base_arn(region=None, profile=None):
    """ Retrieve the base ARN of DynamoDB.
    Kwargs:
        region (str): The AWS region.
        profile (str): The ~/.aws/credentials profile name to use

    Basic Usage:
        >>> base_arn = get_dynamodb_base_arn(us-west-2')
        arn:aws:dynamodb:us-west-2:12345678910:table

    Returns:
        String
    """
    client = aws_client(region, 'dynamodb', profile)
    try:
        tables = client.list_tables(Limit=1)
        table = tables['TableNames'][0]
        arn = client.describe_table(TableName=table)['Table']['TableArn']
        base_arn = arn.split('/')[:-1]
        return base_arn[0]
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                "Unable to find 1 DynamoDB Table"
            )


@AWSRetry.backoff()
def get_kinesis_stream_arn(stream_name, region=None, profile=None):
    """ Retrieve the ARN of a kinesis stream.
    Args:
        stream_name (str): The name of the Kinesis stream.
    Kwargs:
        region (str): The AWS region.
        profile (str): The ~/.aws/credentials profile name to use

    Basic Usage:
        >>> arn = get_kinesis_stream_arn('test', us-west-2')
        arn:aws:kinesis:east-side:123456789:stream/test

    Returns:
        String
    """
    client = aws_client(region, 'kinesis', profile)
    try:
        arn = (
            client.describe_stream(
                StreamName=stream_name, Limit=1
            )['StreamDescription']['StreamARN']
        )
        return arn
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                "Unable to find Kinesis Stream {0}".format(stream_name)
            )


@AWSRetry.backoff()
def zones(region=None, profile=None):
    """ Retrieve a list of available zones in a region.
    Kwargs:
        region (str): The AWS region.
        profile (str): The ~/.aws/credentials profile name to use

    Basic Usage:
        >>> az = zones('us-west-2')
        ['us-west-2a', 'us-west-2b', 'us-west-2c']

    Returns:
        List
    """
    client = aws_client(region, 'ec2', profile)
    zone_names = sorted((
        map(
            lambda x: x['ZoneName'],
            client.describe_availability_zones()['AvailabilityZones']
        )
    ))
    return zone_names


@AWSRetry.backoff()
def get_all_vpcs_info_except(except_ids, region=None, profile=None):
    """
    Args:
        except_ids (list): List of vpcs, that you do not want to match against.

    Basic Usage:
        >>> vpc_ids = ['vpc-345621']
        >>> get_all_vpcs_info_except(vpc_ids)
        ['vpc-1234567', 'vpc-97654321']

    Returns:
        List of vpc ids
    """
    vpcs_info = list()
    client = aws_client(region, 'ec2', profile)
    params = {
        'Filters': [
            {
                'Name': 'state',
                'Values': ['available'],
            },
            {
                'Name': 'isDefault',
                'Values': ['false'],
            }
        ]
    }
    vpcs = client.describe_vpcs(**params)
    if vpcs:
        for vpc in vpcs['Vpcs']:
            if vpc['VpcId'] not in except_ids:
                name = ''
                if vpc.get('Tags', None):
                    for tag in vpc['Tags']:
                        if tag.get('Key', None) == 'Name':
                            name = tag.get('Value')

                    vpcs_info.append(
                        {
                            'name': name,
                            'id': vpc['VpcId'],
                            'cidr': vpc['CidrBlock'],
                        }
                    )
    if vpcs_info:
        return vpcs_info
    else:
        raise errors.AnsibleFilterError("No vpcs were found")


@AWSRetry.backoff()
def get_rds_endpoint(region, instance_name, profile=None):
    """Retrieve RDS Endpoint Address.
    Args:
        region (str): The AWS region.
        instance_name (str): The rds instance name.

    Basic Usage:
        >>> instance_name = 'db-dev'
        >>> get_rds_endpoint('us-west-2', instance_name)
        db-dev.absdefg.us-west-2.rds.amazon.com

    Returns:
        String
    """
    client = aws_client(region, 'rds', profile)
    try:
        rds_instances = (
            client.describe_db_instances(
                DBInstanceIdentifier=instance_name
            )['DBInstances']
        )
        if len(rds_instances) == 1:
            return rds_instances[0]['Endpoint']['Address']
        else:
            raise errors.AnsibleFilterError("More than rds 1 instance found")
    except Exception as e:
        raise errors.AnsibleFilterError(
            "DBInstance {0} not found: {1}".format(instance_name, str(e))
        )


@AWSRetry.backoff()
def get_rds_hosted_zone_id(region, instance_name, profile=None):
    """Retrieve RDS Hosted Zone ID.
    Args:
        region (str): The AWS region.
        instance_name (str): The rds instance name.

    Basic Usage:
        >>> instance_name = 'db-dev'
        >>> get_rds_endpoint('us-west-2', instance_name)
        db-dev.absdefg.us-west-2.rds.amazon.com

    Returns:
        String
    """
    client = aws_client(region, 'rds', profile)
    try:
        rds_instances = (
            client.describe_db_instances(
                DBInstanceIdentifier=instance_name
            )['DBInstances']
        )
        if len(rds_instances) == 1:
            return rds_instances[0]['Endpoint']['HostedZoneId']
        else:
            raise errors.AnsibleFilterError("More than rds 1 instance found")
    except Exception as e:
        raise errors.AnsibleFilterError(
            "DBInstance {0} not found: {1}".format(instance_name, str(e))
        )


@AWSRetry.backoff()
def get_route_table_ids(vpc_id, main='false', region=None, profile=None):
    """
    Args:
        vpc_id (str): The vpc id in which the subnet you are looking
            for lives in,

    Basic Usage:
        >>> vpc_id = 'vpc-12345678'
        >>> get_route_table_ids(vpc_id)
        ['rtb-1234567a']

    Returns:
        List of route table ids
    """
    route_ids = list()
    client = aws_client(region, 'ec2', profile)
    params = {
        'Filters': [
            {
                'Name': 'vpc-id',
                'Values': [vpc_id],
            },
            {
                'Name': 'association.main',
                'Values': [main]
            }
        ]
    }
    routes = client.describe_route_tables(**params)
    if routes:
        route_ids = (
            map(lambda route: route['RouteTableId'], routes['RouteTables'])
        )
        return route_ids
    else:
        raise errors.AnsibleFilterError("No routes were found")


@AWSRetry.backoff()
def get_all_route_table_ids(region, profile=None):
    """
    Args:
        vpc_id (str): The vpc you want to exclude routes from.

    Basic Usage:
        >>> get_all_route_table_ids("us-west-2")
        ['rtb-1234567']

    Returns:
        List of route table ids
    """
    route_ids = list()
    client = aws_client(region, 'ec2', profile)
    params = {
        'Filters': [
            {
                'Name': 'association.main',
                'Values': ['false']
            }
        ]
    }
    routes = client.describe_route_tables(**params)
    if routes:
        for route in routes['RouteTables']:
            route_ids.append(route['RouteTableId'])
        return route_ids
    else:
        raise errors.AnsibleFilterError("No routes were found")


@AWSRetry.backoff()
def get_all_route_table_ids_except(vpc_id, region=None, profile=None):
    """
    Args:
        vpc_id (str): The vpc you want to exclude routes from.

    Basic Usage:
        >>> vpc_id = 'vpc-98c797fd'
        >>> get_all_route_table_ids_except(vpc_id)
        ['rtb-5f78343a']

    Returns:
        List of route table ids
    """
    route_ids = list()
    client = aws_client(region, 'ec2', profile)
    params = {
        'Filters': [
            {
                'Name': 'association.main',
                'Values': ['false']
            }
        ]
    }
    routes = client.describe_route_tables(**params)
    if routes:
        for route in routes['RouteTables']:
            if route['VpcId'] != vpc_id:
                route_ids.append(route['RouteTableId'])
        if len(route_ids) > 0:
            return route_ids
        else:
            raise errors.AnsibleFilterError("No routes were found")
    else:
        raise errors.AnsibleFilterError("No routes were found")


@AWSRetry.backoff()
def get_vpc_ids_from_names(vpc_names, region=None, profile=None):
    """Return a list of vpc ids from the list of vpc names that were matched.
    Args:
        vpc_names (list): List of vpc names you are searching for.
        client (Boto3.Client): The instantiated boto3 client.
    """
    vpc_ids = list()
    client = aws_client(region, 'ec2', profile)
    vpcs = client.describe_vpcs()
    for vpc in vpcs['Vpcs']:
        if 'Tags' in vpc:
            for tag in vpc['Tags']:
                if tag['Key'] == 'Name':
                    for name in vpc_names:
                        if re.search(name, tag['Value'], re.IGNORECASE):
                            vpc_ids.append(vpc['VpcId'])
    return vpc_ids


@AWSRetry.backoff()
def get_all_route_table_ids_except_vpc_names(vpc_names, region=None,
                                             profile=None):
    """
    Args:
        vpc_names (list): List of vpc names you are searching for.

    Kwargs:
        region (str): The AWS region.

    Basic Usage:
        >>> vpc_names = ['test', 'foo']
        >>> get_all_route_table_ids_except_vpc_names(vpc_names)
        ['rtb-123456']

    Returns:
        List of route table ids
    """
    route_ids = list()
    client = aws_client(region, 'ec2', profile)
    vpc_ids = get_vpc_ids_from_names(vpc_names, region, profile)
    params = {
        'Filters': [
            {
                'Name': 'association.main',
                'Values': ['false']
            }
        ]
    }
    routes = client.describe_route_tables(**params)
    if routes:
        for route in routes['RouteTables']:
            if route['VpcId'] not in vpc_ids:
                route_ids.append(route['RouteTableId'])
        if len(route_ids) > 0:
            return route_ids
        else:
            raise errors.AnsibleFilterError("No routes were found")
    else:
        raise errors.AnsibleFilterError("No routes were found")


@AWSRetry.backoff()
def get_all_subnet_ids_in_route_table(route_table_id, region=None,
                                      profile=None):
    """
    Args:
        route_table_id (str): The route id you are retrieving subnets for.

    Kwargs:
        region (str): The AWS region.

    Basic Usage:
        >>> get_all_subnet_ids_in_route_table("rtb-1234567", us-west-2")
        ['subnet-1234567', 'subnet-7654321']

    Returns:
        List of subnet ids
    """
    subnet_ids = list()
    client = aws_client(region, 'ec2', profile)
    params = {
        'RouteTableIds': [route_table_id]
    }
    routes = client.describe_route_tables(**params)
    if routes:
        for route in routes['RouteTables']:
            for association in route['Associations']:
                if association.get('SubnetId', None):
                    subnet_ids.append(association['SubnetId'])
        return subnet_ids
    else:
        raise errors.AnsibleFilterError(
            "No subnets were found for {0}".format(route_table_id)
        )


@AWSRetry.backoff()
def get_subnet_ids_in_zone(vpc_id, zone, region=None, profile=None):
    """
    Args:
        vpc_id (str): The vpc id in which the subnet you are looking
            for lives in,
        zone (str): The region in which the subnet resides.

    Basic Usage:
        >>> vpc_id = 'vpc-12345678'
        >>> aws_region = 'us-west-2'
        >>> zone = 'us-west-2c'
        >>> get_subnet_ids_in_zone(vpc_id, zone, aws_region)
        [u'subnet-4324567', u'subnet-12345678', u'subnet-6543210']

    Returns:
        List of subnet ids
    """
    subnet_ids = list()
    client = aws_client(region, 'ec2', profile)
    params = {
        'Filters': [
            {
                'Name': 'vpc-id',
                'Values': [vpc_id],
            },
            {
                'Name': 'availabilityZone',
                'Values': [zone],
            }
        ]
    }
    subnets = client.describe_subnets(**params)['Subnets']
    if subnets:
        subnet_ids = map(lambda subnet: subnet['SubnetId'], subnets)
        return subnet_ids
    else:
        raise errors.AnsibleFilterError("No subnets were found")


@AWSRetry.backoff()
def get_subnet_ids(vpc_id, cidrs, region=None, profile=None):
    """
    Args:
        vpc_id (str): The vpc id in which the subnet you are looking
            for lives in,
        cidrs (list): The list of cidrs that you are performing the search on.
        region (str): The AWS region.

    Basic Usage:
        >>> cidrs = ['10.100.10.0/24', '10.100.12.0/24', '10.100.11.0/24']
        >>> vpc_id = 'vpc-123456'
        >>> aws_region = 'us-west-2'
        >>> get_subnet_ids(vpc_id, cidrs, aws_region)
        [u'subnet-123456', u'subnet-765432', u'subnet-123456']

    Returns:
        List of subnet ids
    """
    subnet_ids = list()
    client = aws_client(region, 'ec2', profile)

    # in case cidrs is not a list
    if not isinstance(cidrs, list):
        cidrs = [cidrs]

    print cidrs
    params = {
        'Filters': [
            {
                'Name': 'vpc-id',
                'Values': [vpc_id],
            },
            {
                'Name': 'cidrBlock',
                'Values': cidrs,
            }
        ]
    }
    subnets = (
        sorted(
            client.describe_subnets(**params)['Subnets'],
            key=lambda subnet: subnet['AvailabilityZone']
        )
    )
    if subnets:
        subnet_ids = map(lambda subnet: subnet['SubnetId'], subnets)
        return subnet_ids
    else:
        raise errors.AnsibleFilterError("No subnets were found")


@AWSRetry.backoff()
def get_subnet_ids_by_tags(vpc_id, region=None, profile=None, **kwargs):
    """
    Args:
        vpc_id (str): The vpc id in which the subnet you are looking
            for lives in,
        cidrs (list): The list of cidrs that you are performing the search on.
        region (str): The AWS region.

    Basic Usage:
        >>> cidrs = ['10.100.10.0/24', '10.100.12.0/24', '10.100.11.0/24']
        >>> vpc_id = 'vpc-123456'
        >>> aws_region = 'us-west-2'
        >>> get_subnet_ids(vpc_id, cidrs, aws_region)
        [u'subnet-123456', u'subnet-765432', u'subnet-123456']

    Returns:
        List of subnet ids
    """
    client = aws_client(region, 'ec2', profile)

    filters = [{
        'Name': 'vpc-id',
        'Values': [vpc_id]
    }]

    for key, value in kwargs.iteritems():
        if not isinstance(value, list):
            value = [value]
        filters.append({
            'Name': 'tag:' + key,
            'Values': value
        })

    subnets = (
        sorted(
            client.describe_subnets(Filters=filters)['Subnets'],
            key=lambda subnet: subnet['AvailabilityZone']
        )
    )
    if subnets:
        subnet_ids = map(lambda subnet: subnet['SubnetId'], subnets)
        return subnet_ids
    else:
        raise errors.AnsibleFilterError("No subnets were found")


@AWSRetry.backoff()
def get_vpc_id_by_name(name, region, profile=None):
    """
    Args:
        name (str): The name of the vpc you are retrieving the id for.
        region (str): The AWS region.

    Basic Usage:
        >>> vpc_name = 'test'
        >>> aws_region = 'us-west-2'
        >>> vpc_id = get_vpc_id_by_name(vpc_name, aws_region)
        'vpc-1234567'

    Returns:
        VPC ID
    """
    client = aws_client(region, 'ec2', profile)
    params = {
        "Filters": [
            {
                "Name": "tag-key",
                "Values": ["Name"]
            },
            {
                "Name": "tag-value",
                "Values": [name]
            }
        ]
    }
    try:
        vpc_id = client.describe_vpcs(**params)['Vpcs'][0]['VpcId']
        return vpc_id

    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise(e)
        else:
            raise errors.AnsibleFilterError(
                "VPC ID for VPC name {0} was not found in region {1}: {2}"
                .format(name, region, str(e))
            )


def vpc_exists(name, region):
    """
    Args:
        name (str): The name of the vpc you are retrieving the id for.
        region (str): The AWS region.

    Basic Usage:
        >>> vpc_name = 'test'
        >>> aws_region = 'us-west-2'
        >>> vpc_id = vpc_exists(vpc_name, aws_region)
        'vpc-1234567'

    Returns:
        VPC ID
    """
    vpc_id = None
    try:
        vpc_id = get_vpc_id_by_name(name, region)
    except Exception:
        vpc_id = 'does not exist'
    return vpc_id


@AWSRetry.backoff()
def get_ami_images(name, region, arch="x86_64", virt_type="hvm",
                   owner="099720109477", sort=False, sort_by="creationDate",
                   sort_by_tag=False, tags=None, order="desc",
                   fail_if_empty=False):
    """
    Args:
        name (str): The name of the of the image you are searching for.
        region (str): The AWS region.
    Kwargs:
        arch (str): The architecture of the image (i386|x86_64)
            default=x86_64
        virt_type (str): (hvm|pv)
        owner (str): The owner of the image (me|amazon|099720109477) etc...
            default=099720109477 (This is Canonical)
        sort (bool): If you know the search is going to return multiple images,
            than you can sort based on an attribute of the ami image you are
            looking for. default=False
        sort_by (str): The instance attribute or tag key you want to sort on.
        sort_by_tag (bool): In order to sort by tag, this arguments needs
            to be flagged as True. default=False
        tags (list of tuples): Filter base on multiple tags.
            example.. tags=[(State, current)]
            default=None
        order (str): asc or desc. default=desc

    Basic Usage:
        >>> name = 'ubuntu/images/hvm/ubuntu-trusty-14.04-amd64-server-20150609'
        >>> aws_region = 'us-west-2'
        >>> images = get_ami_images(name, aws_region)
        [Image:ami-1234567]

    Returns:
        List of image ids
    """
    reverse = False
    filter_by = {}
    if isinstance(tags, list):
        for key, val in tags:
            key = "tag:{0}".format(key)
            filter_by[key] = val

    filter_by.update(
        {
            "name": name,
            "architecture": arch,
            "virtualization_type": virt_type
        }
    )
    if order == "desc":
        reverse = True

    connect = boto.ec2.connect_to_region(region)
    images = connect.get_all_images(owners=owner, filters=filter_by)
    if images:
        if sort:
            if sort_by_tag:
                images.sort(key=lambda x: x.tags[sort_by], reverse=reverse)
            else:
                images.sort(key=lambda x: getattr(x, sort_by), reverse=reverse)
    elif len(images) == 0 and fail_if_empty:
        raise errors.AnsibleFilterError(
            "No instance was found with name {0} in region {1}"
            .format(name, region)
        )
    return images


@AWSRetry.backoff()
def get_instances_by_tags(region, return_key="PrivateIpAddress",
                          state=None, profile=None, **tags):
    """Retrieve instances by 1 or multiple tags.
    Args:
        region (str): The AWS region.
        tags (dict): The tags you want to filter by.

    Kwargs:
        return_key (str): the property of the instance you want to return.
            default=PublicIpAddress
        state (str): A valid instance state to add to the search filter.
            The following are valid states: pending, running, stopped,
            stopping, rebooting, shutting-down, terminated.
            default=None

    Basic Usage:
        >>> region = 'us-west-2'
        >>> ip_addresses = get_instances_by_tags(
                region, service='super-turbo-webapp', env='foobar')
        [u'10.0.0.101']

    Returns:
        String
    """
    filters = list()
    instances = list()
    client = aws_client(region, 'ec2', profile)
    for key, val in tags.items():
        filters.append(
            {
                'Name': "tag:{0}".format(key),
                'Values': [val]
            }
        )

    if state:
        filters.append(
            {
                'Name': "instance-state-name",
                'Values': [state]
            }
        )
    try:
        reservations = client.describe_instances(Filters=filters)\
                ['Reservations']
        for reservation in reservations:
            for instance in reservation['Instances']:
                instances.append(instance.get(return_key))

        return instances
    except Exception as e:
        raise e


@AWSRetry.backoff()
def get_instance_by_tags(region, return_key="PrivateIpAddress",
                         state=None, profile=None, **tags):

    instances = get_instances_by_tags(region, return_key, state, profile, **tags)
    if len(instances) > 1:
        raise errors.AnsibleFilterError(
            "More than 1 {0} instance was found with the following tags {1} in region {2}"
            .format(instances, tags, region)
        )
    elif len(instances) == 1:
        return instances[0]
    else:
        raise errors.AnsibleFilterError(
            "No instances was found with the following tags {0} in region {1}"
            .format(tags, region)
        )


@AWSRetry.backoff()
def get_instance(name, region, return_key="ip_address", state=None,
                 tag_name='Name', ignore_tag_key=None):
    """
    Args:
        name (str): The name of the instance id you are retrieving the key for.
        region (str): The AWS region.

    Kwargs:
        return_key (str): the property of the instance you want to return.
            default=ip_address
        state (str): A valid instance state to add to the search filter.
            The following are valid states: pending, running, stopped,
            stopping, rebooting, shutting-down, terminated.
            default=None
        tag_name (str): The tag key you want to use to search by.
            default=Name
        ignore_tag_key (str): ignore any instances that contain this key.

    Basic Usage:
        >>> name = 'base'
        >>> region = 'us-west-2'
        >>> ip_address = get_instance(name, region)
        u'10.0.0.101'

    Returns:
        String
    """
    filter_by = {
        "tag:{0}".format(tag_name): name,
    }
    if state:
        filter_by["instance-state-name"] = state
    try:
        connect = boto.ec2.connect_to_region(region)
        images = connect.get_all_instances(filters=filter_by)
        if len(images) == 1:
            instance = images[0].instances[0]
            result = getattr(instance, return_key)
            return result
        elif len(images) > 1:
            if ignore_tag_key:
                does_not_have_tag_instances = list()
                for image in images:
                    if ignore_tag_key not in image.instances[0].tags:
                        instance = images[0].instances[0]
                        result = getattr(instance, return_key)
                        does_not_have_tag_instances.append(result)
                if len(does_not_have_tag_instances) == 1:
                    return does_not_have_tag_instances[0]
            raise errors.AnsibleFilterError(
                "More than 1 instance was found with name {0} in region {1}"
                .format(name, region)
            )
        elif len(images) == 0:
            raise errors.AnsibleFilterError(
                "No instance was found with name {0} in region {1}"
                .format(name, region)
            )
    except Exception as e:
        raise e


def get_older_images(name, region, exclude_ami=None,
                     exclude_archived=True, **kwargs):
    """
    Args:
        name (str): The name of the instance id you are retrieving the key for.
        region (str): The AWS region.

    Kwargs:
        exclude_ami (str): The ami_image you would like to exclude from
            this search. default=None
        exclude_archived (bool): Do not include images that are already
            tagged with ArchiveDate. default=True

    """
    owner = "self"
    images = get_ami_images(name, region, owner=owner, **kwargs)
    image_ids = []
    amis_that_are_already_tagged = []
    if images:
        if exclude_archived:
            for ami in images:
                if 'ArchivedDate' in ami.tags:
                    amis_that_are_already_tagged.append(ami.id)
        image_ids = map(lambda image: image.id, images)
        if exclude_ami in image_ids:
            image_ids.remove(exclude_ami)
    return list(set(image_ids).difference(amis_that_are_already_tagged))


def latest_ami_id(name, region, ami_owner_id='099720109477'):
    """Retrieve the last ami that was created with name.
    Args:
        name (str): The name of the instance id you are retrieving the key for.
        region (str): The AWS region.
        ami_owner_id (str): Owner ID of the AMI. get_ami_images() default to 099720109477

    """
    images = (
        get_ami_images(
            name, region, owner=ami_owner_id, sort=True, sort_by='creationDate',
            order='desc'
        )
    )

    return images[0].id


def get_ami_image_id(name, region, **kwargs):
    """
    Args:
        name (str): The name of the of the image you are searching for.
        region (str): The AWS region.

    Basic Usage:
        >>> name = 'ubuntu/images/hvm/ubuntu-trusty-14.04-amd64-server-20150609'
        >>> aws_region = 'us-west-2'
        >>> get_ami_image_id(name, aws_region)
        u'ami-1234567'

    Returns:
        AMI Image Id
    """

    images = get_ami_images(name, region, **kwargs)

    if len(images) == 1:
        return images[0].id

    elif len(images) > 1:
        raise errors.AnsibleFilterError(
            "More than 1 instance was found with name {0} in region {1}"
            .format(name, region)
        )
    else:
        raise errors.AnsibleFilterError(
            "No instance was found with name {0} in region {1}"
            .format(name, region)
        )


def get_instance_id_by_name(name, region, state="running"):
    instance_id = (
        get_instance(name, region, return_key="id", state=state)
    )
    return instance_id


@AWSRetry.backoff()
def get_acm_arn(domain_name, region, profile=None):
    """Retrieve the attributes of a certificate if it exists or all certs.
    Args:
        domain_name (str): The domain name of the certificate.
        region (str): The AWS region.

    Basic Usage:
        >>> arn = get_acm_arn('test', 'us-west-2')
        "arn:aws:acm:us-west-2:123456789:certificate/25b4ad8a-1e24-4001-bcd0-e82fb3554cd7",
    """
    arn = None
    client = aws_client(region, 'acm', profile)
    try:
        acm_certs = client.list_certificates()['CertificateSummaryList']
        for cert in acm_certs:
            if domain_name == cert['DomainName']:
                arn = cert['CertificateArn']
                return arn
        if not arn:
            raise errors.AnsibleFilterError(
                'Certificate {0} does not exist'.format(domain_name)
            )
    except Exception as e:
        raise e


@AWSRetry.backoff()
def get_acm_arn_by_tag_name(region, tag_name, profile=None):
    """Retrieve the attributes of a certificate if it exists or all certs.
    Args:
        region (str): The AWS region.
        tag_name (str): The domain name of the certificate.

    Basic Usage:
        >>> arn = get_acm_arn('wildcard.traveloka.com', 'ap-southeast-1')
        "arn:aws:acm:us-west-2:123456789:certificate/25b4ad8a-1e24-4001-bcd0-e82fb3554cd7",
    """
    client = aws_client(region, 'acm', profile)
    try:
        acm_certs = client.list_certificates()['CertificateSummaryList']
        for cert in acm_certs:
             arn = cert['CertificateArn']
             for tag in client.list_tags_for_certificate(CertificateArn=arn)['Tags']:
                if tag['Key'] == 'Name' and tag['Value'] == tag_name:
                    return arn

        raise errors.AnsibleFilterError(
            'Certificate {0} does not exist'.format(tag_name)
        )
    except Exception as e:
        raise e


@AWSRetry.backoff()
def get_elasticache_endpoint(region, name, profile=None):
    """Retrieve the endpoint name of the elasticache cluster.
    Args:
        region (str): The AWS region.
        name (str): The name of the elasticache cluster.

    Basic Usage:
        >>> endpoint = get_elasticache_endpoint('us-west-2', 'test')
        dns_name
    """
    client = aws_client(region, 'elasticache', profile)
    try:
        return client.describe_cache_clusters(CacheClusterId=name)['CacheClusters'][
            0]['ConfigurationEndpoint']['Address']
    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                'Could not retrieve ip for {0}: {1}'.format(name, str(e))
            )


@AWSRetry.backoff()
def get_instance_tag_name_by_ip(region, ip, ip_type='private', profile=None):
    """
    Args:
        region (str): The AWS region.
        ip (str): The internal or public ip address.

    Kwargs:
        ip_type (str): private or public. default=private
        profile (str): The aws profile name that is set in ~/.aws/credentials

    Basic Usage:
        >>> get_instance_tag_name_by_ip('us-west-2', '10.10.10.10', 'private')
        foobar
    """
    client = aws_client(region, 'ec2', profile)
    filters = list()
    try:
        if ip_type == 'private':
            filter_by = 'private-ip-address'
        else:
            filter_by = 'ip-address'

        filters.append(
            {
                'Name': filter_by,
                'Values': [ip]
            }
        )

        instance = (
            client
            .describe_instances(
                Filters=filters
            )['Reservations'][0]['Instances'][0]
        )
        for tag in instance['Tags']:
            if tag['Key'] == 'Name':
                return tag['Value']

    except Exception as e:
        if isinstance(e, botocore.exceptions.ClientError):
            raise e
        else:
            raise errors.AnsibleFilterError(
                'Could not retrieve tag name for {0}: {1}'.format(ip, str(e))
            )

@AWSRetry.backoff()
def get_instances_tag_name_by_tags(region=None,
                          state=None, profile=None, **tags):

    """Retrieve instances tag Name by 1 or multiple tags.

    """
    filters = list()
    instances = list()
    client = aws_client(region, 'ec2', profile)
    for key, val in tags.items():
        filters.append(
            {
                'Name': "tag:{0}".format(key),
                'Values': [val]
            }
        )

    if state:
        filters.append(
            {
                'Name': "instance-state-name",
                'Values': [state]
            }
        )
    try:
        reservations = client.describe_instances(Filters=filters)\
                ['Reservations']
        for reservation in reservations:
            for instance in reservation['Instances']:
                for tag in instance['Tags']:
                    if tag['Key'] == 'Name':
                        instances.append(tag['Value'])

        if not reservations:
            raise errors.AnsibleFilterError(
                "No instance was found with the following tags {0} in region {1}" .format(
                    tags, region))
        else:
            return instances
    except Exception as e:
        raise e


class FilterModule(object):
    ''' Ansible core jinja2 filters '''

    def filters(self):
        return {
            'get_vpc_id_by_name': get_vpc_id_by_name,
            'get_ami_image_id': get_ami_image_id,
            'get_instance_id_by_name': get_instance_id_by_name,
            'get_subnet_ids': get_subnet_ids,
            'get_subnet_ids_by_tags': get_subnet_ids_by_tags,
            'get_sg': get_sg,
            'get_sg_ids_by_names': get_sg_ids_by_names,
            'get_sg_cidrs': get_sg_cidrs,
            'get_sgs_by_tags': get_sgs_by_tags,
            'get_sg_by_tags': get_sg_by_tags,
            'get_older_images': get_older_images,
            'get_instance': get_instance,
            'get_all_vpcs_info_except': get_all_vpcs_info_except,
            'get_route_table_ids': get_route_table_ids,
            'get_all_route_table_ids': get_all_route_table_ids,
            'get_all_route_table_ids_except': get_all_route_table_ids_except,
            'get_subnet_ids_in_zone': get_subnet_ids_in_zone,
            'latest_ami_id': latest_ami_id,
            'get_rds_endpoint': get_rds_endpoint,
            'get_rds_hosted_zone_id': get_rds_hosted_zone_id,
            'zones': zones,
            'get_sqs': get_sqs,
            'get_instance_profile': get_instance_profile,
            'get_server_certificate': get_server_certificate,
            'vpc_exists': vpc_exists,
            "get_dynamodb_base_arn": get_dynamodb_base_arn,
            'get_kinesis_stream_arn': get_kinesis_stream_arn,
            'get_account_id': get_account_id,
            'get_instance_by_tags': get_instance_by_tags,
            'get_instances_by_tags': get_instances_by_tags,
            'get_acm_arn': get_acm_arn,
            'get_elasticache_endpoint': get_elasticache_endpoint,
            'get_vpc_ids_from_names': get_vpc_ids_from_names,
            'get_instance_tag_name_by_ip': get_instance_tag_name_by_ip,
            'get_instances_tag_name_by_tags': get_instances_tag_name_by_tags,
            'get_acm_arn_by_tag_name': get_acm_arn_by_tag_name
        }
