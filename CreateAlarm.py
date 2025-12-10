import boto3
from datetime import datetime, timedelta



# AWS SDK 클라이언트 초기화
ec2 = boto3.client('ec2')
cloudwatch = boto3.client('cloudwatch')

# Lambda_handler 구현
def lambda_handler(event, context):
    """
    EC2 인스턴스가 Auto Scaling Group에 의해 생성되었을 때 알람을 자동으로 설정하는 Lambda 핸들러
    """
    if event.get('detail', {}).get('eventName') != 'RunInstances':
        return

    response_elements = event.get('detail', {}).get('responseElements')
    if not response_elements:
        return

    instances_set = response_elements.get('instancesSet', {})
    instances = instances_set.get('items', [])
    if not instances:
        return

    for instance in instances:
        instance_id = instance['instanceId']

        # EC2 Name 태그 추출
        name_tags = ec2.describe_tags(Filters=[
            {'Name': 'resource-id', 'Values': [instance_id]},
            {'Name': 'key', 'Values': ['Name']}
        ]).get('Tags', [])
        instance_name = next((tag['Value'] for tag in name_tags if tag['Key'] == 'Name'), instance_id)

        # AutoScalingGroup 태그 추출 및 asg-smk3 여부 확인
        asg_tags = ec2.describe_tags(Filters=[
            {'Name': 'resource-id', 'Values': [instance_id]},
            {'Name': 'key', 'Values': ['aws:autoscaling:groupName']}
        ]).get('Tags', [])
        asg_name = next((t['Value'] for t in asg_tags if t['Key'] == 'aws:autoscaling:groupName'), None)

        if not (asg_name and asg_name.startswith('CodeDeploy_')):
            continue

        alarm_tags = ec2.describe_tags(Filters=[
            {'Name': 'resource-id', 'Values': [instance_id]},
            {'Name': 'key', 'Values': ['Alarm']}
        ]).get('Tags', [])
        if not any(tag['Key'] == 'Alarm' and tag['Value'] == 'Y' for tag in alarm_tags):
            continue

        # 표준 EC2 메트릭 알람 생성
        create_alarm(instance_name, instance_id, 'CPUUtilization', 'AWS/EC2', 'Percent', 80.0, 'Average')
        create_alarm(instance_name, instance_id, 'StatusCheckFailed', 'AWS/EC2', 'Count', 1.0, 'Maximum')
        # CWAgent 메트릭 알람 생성 (메모리, 디스크 사용률)
        create_alarm(instance_name, instance_id, 'mem_used_percent', 'CWAgent', 'Percent', 80.0, 'Average')
        create_alarm(instance_name, instance_id, 'disk_used_percent', 'CWAgent', 'Percent', 80.0, 'Average')


def create_alarm(instance_name, instance_id, metric_name, namespace, unit, threshold, statistic):
    """
    인스턴스 정보 기반으로 CloudWatch 알람 생성을 위한 Dimension 구성 및 호출
    """
    alarm_name = f'{instance_name}-{instance_id}-{metric_name}'

    # EC2 인스턴스 상세 정보 가져오기
    ec2_info = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = ec2_info.get('Reservations', [])
    if not reservations or not reservations[0]['Instances']:
        print(f"[ERROR] 인스턴스 정보를 가져올 수 없음: {instance_id}")
        return

    instance_info = reservations[0]['Instances'][0]
    image_id = instance_info['ImageId']
    instance_type = instance_info['InstanceType']

    # AutoScalingGroupName은 태그로부터 가져오기
    tags = instance_info.get('Tags', [])
    asg_name = next((tag['Value'] for tag in tags if tag['Key'] == 'aws:autoscaling:groupName'), None)

    # CWAgent namespace인 경우, 전체 Dimension 조합 사용
    if namespace == "CWAgent" and asg_name:
        dimensions = [
            {'Name': 'InstanceId', 'Value': instance_id},
            {'Name': 'AutoScalingGroupName', 'Value': asg_name},
            {'Name': 'ImageId', 'Value': image_id},
            {'Name': 'InstanceType', 'Value': instance_type}
        ]

        # 디스크 사용률 알람인 경우 추가 Dimension 필요
        if metric_name == "disk_used_percent":
            dimensions += [
                {'Name': 'path', 'Value': '/'},                 # 일반적으로 루트 디스크 기준
                {'Name': 'device', 'Value': 'nvme0n1p1'},       # EC2 유형에 따라 달라질 수 있음 (사용자 서버 내 df 명령을 통해 /와 연계된 device 값을 넣어줌)(rootfs가 표현되면 /의 심볼릭 링크이므로  rootfs로 넣어주면 관리가 용이)
                {'Name': 'fstype', 'Value': 'xfs'}              # AL2, AL2023은 xfs가 일반적 (사용자 서버 내 df 명령을 통해 /와 연계된 fstype 값을 넣어줌)(rootfs가 표현되면 /의 심볼릭 링크이므로  rootfs로 넣어주면 관리가 용이)
            ]
    else:
        # AWS/EC2 표준 메트릭은 InstanceId만 사용
        dimensions = [{'Name': 'InstanceId', 'Value': instance_id}]

    _create_alarm(alarm_name, metric_name, namespace, dimensions, unit, threshold, statistic)


def _create_alarm(alarm_name, metric_name, namespace, dims, unit, threshold, statistic):
    """
    CloudWatch put_metric_alarm 호출을 통한 알람 실제 생성 로직
    """
    print(f"[CREATE] Attempting to create alarm: {alarm_name}")
    try:
        existing = cloudwatch.describe_alarms(AlarmNames=[alarm_name])
        if existing['MetricAlarms']:
            print(f"[SKIP] Alarm already exists: {alarm_name}")
            return

        cloudwatch.put_metric_alarm(
            AlarmName=alarm_name,
            ComparisonOperator='GreaterThanThreshold',
            EvaluationPeriods=2,
            MetricName=metric_name,
            Namespace=namespace,
            Period=300,
            Statistic=statistic,
            Threshold=threshold,
            ActionsEnabled=False,  # SNS 연동 시 True로 변경
            # AlarmActions=[
            #     'arn:aws:sns:ap-northeast-2:983460235393:sns-smk-topic'  # ✅ SNS ARN 추가
            # ],
            AlarmDescription=f'{metric_name} alarm',
            Dimensions=dims,
            Unit=unit
        )
        print(f"[SUCCESS] Created alarm: {alarm_name}")
    except Exception as e:
        print(f"[ERROR] Failed to create alarm {alarm_name}: {e}")
