# adc-pycsw
Setup and deployment of pycsw for the Arctic Data Centre (ADC) project

## Installation 

- Clone pycsw

```git clone https://github.com/geopython/pycsw```

- Clone adc-pycsw (this repository)

```git clone https://github.com/epifanio/adc-pycsw```

- Copy the plugin files into the `pycsw` source code

```
cp adc-pycsw/plugins/repository/solr_metno.py pycsw/pycsw/plugins/repository/
```

```
cp adc-pycsw/plugins/repository/solr_helper.py pycsw/pycsw/plugins/repository/
```

- Copy the output profiles files into the `pycsw` source code

```
cp adc-pycsw/plugins/outputschemas/*py pycsw/pycsw/plugins/outputschemas/
```

- Create a python environment (using conda for convenience)

```conda create -n pycswdev python==3.9```

- Activate newly created environment

```conda activate pycswdev```

- Install pycsw and its dependencies


```
cd pycsw
pip install -r requirements-dev.txt
pip install -e .
```

## Setup


- make a copy of the pycsw configuration example file `default-sample.cfg` and name it `default.cfg` edit its content to have the following in the `server` and `repository` section:

```
[server]
home=/path/to/pycsw
url=http://localhost:8000/pycsw/csw.py
```

```
[repository]
database=None
table=None
source=pycsw.plugins.repository.solr_metno.SOLRMETNORepository
filter=http://solr:8983/solr/adc
adc_collection=ADC,NBS
MMD_XSL_DIR=/usr/local/share/mmd/xslt/

[xslt]
mmd_to_iso=/usr/local/share/mmd/xslt/mmd-to-inspire.xsl
dif=/usr/local/share/mmd/xslt/mmd-to-dif.xsl
dif10=/usr/local/share/mmd/xslt/mmd-to-dif10.xsl
wmo=/usr/local/share/mmd/xslt/mmd-to-wmo.xsl
mmd_to_inspire=/usr/local/share/mmd/xslt/mmd-to-inspire.xsl

```

- from the `pycswdev` environment export the ```MMD_TO_ISO``` environment variable, to the path for the xslt used to convert MMD records to ISO, e.g.:

```export MMD_TO_ISO="mmd/xslt/mmd-to-inspire.xsl"```


- activate the pycswdev environment and run the pycsw wsgi app
```
conda activate pycswdev
python ./pycsw/wsgi.py
```
pray!

## Some info for later development
[QueryRequestExamplesGist](https://gist.github.com/kalxas/6ecb06d61cdd487dc7f9)
