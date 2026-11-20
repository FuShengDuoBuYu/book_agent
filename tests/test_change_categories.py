import unittest
from copy import deepcopy
from datetime import date
from app.tools.financial_analysis import run_analysis


class ChangeCategoryTests(unittest.TestCase):
    def test_expense_drivers_keep_generic_category_while_showing_original_remarks(self):
        rows=[{'year':2026,'month':m,'day':d,'money':amount,'costType':'其他','orderRemark':note}
              for m,d,amount,note in [(9,1,-20,'咖啡'),(9,2,-30,'书籍'),(10,1,-10,'咖啡'),(10,2,-50,'车票')]]
        before=deepcopy(rows)
        result=run_analysis('changes',rows,as_of=date(2026,11,20))
        driver,=result['expenseDrivers']
        self.assertEqual(driver['name'],'其他')
        self.assertEqual((driver['before'],driver['after'],driver['change']),(50,60,10))
        self.assertEqual((driver['beforeCount'],driver['afterCount']),(2,2))
        self.assertEqual(set(driver['afterRemarks']),{'咖啡','车票'})
        self.assertEqual(result['expenseChange'],10)
        self.assertEqual(rows,before)

    def test_unclassified_expenses_are_a_category_and_income_still_uses_sources(self):
        rows=[{'year':2026,'month':m,'day':d,'money':amount,'costType':category,'orderRemark':note}
              for m in (9,10) for d,amount,category,note in [(1,-20,'','日常项目'),(2,300,'收入','校稿'),(3,400,'收入','课程')]]
        result=run_analysis('changes',rows,as_of=date(2026,11,20))
        self.assertEqual([r['name'] for r in result['expenseDrivers']],['未分类'])
        self.assertEqual({r['name'] for r in result['incomeDrivers']},{'校稿','课程'})

    def test_removed_expense_keeps_previous_period_remark(self):
        rows=[{'year':2026,'month':9,'day':1,'money':-900,'costType':'学习','orderRemark':'学费'},
              {'year':2026,'month':10,'day':1,'money':-20,'costType':'餐饮','orderRemark':'午餐'}]
        result=run_analysis('changes',rows,as_of=date(2026,11,20))
        driver=next(r for r in result['expenseDrivers'] if r['name']=='学习')
        self.assertEqual(driver['beforeRemarks'],['学费'])
        self.assertEqual(driver['afterRemarks'],[])
