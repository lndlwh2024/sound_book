import math
import pytest
from src.utils.resource_sampler import CpuPackagePowerMeter


class FakePdh:
    def __init__(self, value=27606, status=0, add_error=0, read_error=0):
        self.value=value;self.status=status;self.add_error=add_error;self.read_error=read_error
        self.closed=0;self.path=None
    def PdhOpenQueryW(self, unused, user, query):
        query._obj.value=1
        return 0
    def PdhAddEnglishCounterW(self, query, path, user, counter):
        self.path=path;counter._obj.value=2
        return self.add_error
    def PdhCollectQueryData(self, query):return 0
    def PdhGetFormattedCounterValue(self, counter, fmt, kind, value):
        value._obj.status=self.status;value._obj.value=self.value
        return self.read_error
    def PdhCloseQuery(self, query):self.closed+=1


def test_package_power_milliwatts_to_watts_and_cleanup():
    pdh=FakePdh();meter=CpuPackagePowerMeter(pdh)
    assert pdh.path==r"\Energy Meter(RAPL_Package0_PKG)\Power"
    assert meter.read()==pytest.approx(27.606)
    meter.close();meter.close()
    assert pdh.closed==1 and meter.read() is None


@pytest.mark.parametrize('options',[{'value':-1},{'value':math.nan},{'value':math.inf},{'status':99},{'read_error':1},{'add_error':1}])
def test_unavailable_power_is_unknown_not_zero(options):
    pdh=FakePdh(**options);meter=CpuPackagePowerMeter(pdh)
    assert meter.read() is None
    meter.close()
    assert pdh.closed==1


def test_valid_zero_and_recovery_after_invalid_sample():
    pdh=FakePdh(status=99);meter=CpuPackagePowerMeter(pdh)
    assert meter.read() is None
    pdh.status=0;pdh.value=0
    assert meter.read()==0
    pdh.value=12300
    assert meter.read()==12.3
    meter.close()
