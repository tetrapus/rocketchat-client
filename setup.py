from setuptools import setup, find_packages
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()


setup(
    name='rocketchat-client',
    version='0.1.0',  # Required
    description='A python Rocketchat API wrapper',
    long_description=long_description,
    url='https://github.com/tetrapus/rocketchat-client',
    author='Joey Tuong',
    author_email='joey@tetrap.us',

    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Topic :: Communications :: Chat',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
    ],

    keywords='rocketchat api wrapper',
    packages=find_packages(),
    install_requires=['ws4py', 'requests'],
    extras_require={
        'test': ['pytest', 'hypothesis', 'mypy'],
    },
)
