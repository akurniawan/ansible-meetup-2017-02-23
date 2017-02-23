# Filter Plugins

In this repository we could learn how to create new filter plugins in Ansible.

## Requirements
* Ansible
* Python 2.7

## Demo: How to create new ansible filter plugins

1. In order to create new ansible filter plugins, the first thing to do is to create folder base on configured path in `ansible.cfg`.
  Within `ansible.cfg`, you can find field named `filter_plugins`. You can change the value to meet your needs. 
  You can also follow our structure in this repository
2. The only thing you need to create new filter plugin is
  ``` Python
  class FilterModule(object):

    def filters(self):
        return {}
  ```
  You can see the example at `filter_plugins/new_plugins.py`

## Demo: Use filter plugin in AWS to create ec2
1. The intention of this demo is to show how to create ec2 using ansible filter
2. Within this demo you will learn how to dynamically get ami id based on ami's name

## Demo: User filter plugin in AWS to automatically attach ec2 instance after elb has created
1. The intention of this demo is to show how to attach ec2 instance automatically while creating elb

## Playbooks
* `play.yml`
* `play_aws_ec2.yml`
* `play_aws_elb.yml`

# Reference
* https://github.com/linuxdynasty/ld-ansible-filter-plugins/tree/bbd28a8b7ea667e29e5c789306147847a87ef398
