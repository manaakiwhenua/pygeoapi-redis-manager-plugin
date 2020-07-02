from setuptools import setup

setup(name='pygeoapi_redis_manager_plugin',
    version='0.0.1',
    description='pygeoapi plugin using Redis and RQ as a processing queue and persistent storage',
    url='https://github.com/manaakiwhenua/pygeoapi-redis-manager-plugin',
    author='Richard Law',
    author_email='lawr@landcareresearch.co.nz',
    license='MIT',
    packages=['pygeoapi_redis_manager_plugin'],
    install_requires=[
        'redis~=3.5.3', 'rq~=1.4.3', 'click~=7.1.2'
    ],
    zip_safe=False
)
