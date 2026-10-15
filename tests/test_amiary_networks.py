import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location("networks",Path(__file__).resolve().parents[1]/"scripts/verify-amiary-networks.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Networks(unittest.TestCase):
    def test_exact_encrypted_overlay(self):
        valid=dict(Name="fixture",Driver="overlay",Scope="swarm",Options={"encrypted":""},Attachable=True)
        m.validate(valid,"fixture")
        for change in (dict(Name="wrong"),dict(Driver="bridge"),dict(Scope="local"),dict(Options={}),dict(Options=None),dict(Attachable=False)):
            with self.subTest(change=change),self.assertRaises(ValueError):m.validate(dict(valid,**change),"fixture")
if __name__=="__main__":unittest.main()
