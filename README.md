# Filter Plugins

In this repository we could learn how to create new filter plugins in Ansible.

## Requirements
* Ansible
* Python 2.7

## How to create new ansible filter plugins

1. In order to create new ansible filter plugins, the first thing to do is to create folder base on configured path in `ansible.cfg`.
  Within `ansible.cfg`, you can find field named `filter_plugins`. You can change the value to meet your needs. 
  You can also follow our structure in this repository
  ```
  repo
  ├── ansible.cfg 
  ├── filter_plugins
  │   ├── aws.py
  │   └── new_plugins.py
  ├── playbooks
  │   ├── play_aws.yml
  │   └── play.yml
  └── README.md
  ```
2. The only thing you need to create new filter plugin is
  ``` Python
  class FilterModule(object):

    def filters(self):
        return {}
  ```
  You can see the example at `filter_plugins/new_plugins.py`
  
## Why filter plugins?

# Reference
* https://github.com/linuxdynasty/ld-ansible-filter-plugins/tree/bbd28a8b7ea667e29e5c789306147847a87ef398
